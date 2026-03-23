"""
Luau Decompiler Discord Bot
Uses the official Roblox luau CLI (built from source) to decompile .luau bytecode files
Setup: pip install -r requirements.txt && python bot.py
"""

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands
import asyncio
import io
import os
import tempfile
import subprocess
import logging
import time
import traceback

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")
_HERE        = os.path.dirname(os.path.abspath(__file__))
LUAU_BINARY  = os.getenv("LUAU_BIN", "luau")
MAX_FILE_SIZE = 2_000_000   # 2 MB
TIMEOUT_SECS  = 60

# Semaphore — 1 decompile at a time to avoid CPU starvation on free Render
_decompile_semaphore = asyncio.Semaphore(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("decompiler-bot")

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ── Binary check ──────────────────────────────────────────────────────────────
def check_luau():
    global LUAU_BINARY
    candidates = [LUAU_BINARY] + [
        p for p in [
            "/opt/render/project/src/vendor/bin/luau",
            os.path.join(_HERE, "vendor", "bin", "luau"),
            "luau",
        ] if p != LUAU_BINARY
    ]
    for candidate in candidates:
        try:
            result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                LUAU_BINARY = candidate
                log.info("luau found: %s → %s", candidate, (result.stdout or result.stderr).strip())
                return
        except (FileNotFoundError, PermissionError):
            continue
    raise RuntimeError(
        f"No luau binary found. Tried: {candidates}\n"
        "On Render: make sure build.sh ran and LUAU_BIN is set.\n"
        "Locally: build from https://github.com/luau-lang/luau"
    )

# ── Core decompile logic ──────────────────────────────────────────────────────
async def run_decompile(bytecode: bytes) -> tuple[str, float]:
    """
    Takes raw luau bytecode, runs luau --decompile, returns (source, elapsed_ms).
    Raises RuntimeError on failure.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, "input.luau")

        with open(in_path, "wb") as f:
            f.write(bytecode)

        cmd = [LUAU_BINARY, "--decompile", in_path]

        t0 = time.perf_counter()

        try:
            async def _run():
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                return proc.returncode, stdout, stderr

            returncode, stdout, stderr = await asyncio.wait_for(_run(), timeout=TIMEOUT_SECS)

        except asyncio.TimeoutError:
            raise RuntimeError(f"Decompile timed out after {TIMEOUT_SECS}s.")

        elapsed = (time.perf_counter() - t0) * 1000

        out_text = stdout.decode(errors="replace").strip()
        err_text = stderr.decode(errors="replace").strip()

        if returncode != 0 or not out_text:
            # Provide helpful context on common failure modes
            hint = ""
            if b"\x1b" in bytecode[:4] or b"RSB1" in bytecode[:8]:
                hint = "\n\nThis looks like **compiled Roblox bytecode** — make sure you're uploading a raw `.luau` bytecode file, not a `.rbxm`/`.rbxl` asset."
            elif bytecode[:4] != b"\x1bLua":
                hint = "\n\nThis doesn't look like valid Luau bytecode. Upload the compiled `.luau` binary output, not plain source code."

            raise RuntimeError(
                f"Decompile failed:\n```\n{(err_text or out_text)[:1500]}\n```{hint}"
            )

        return out_text, elapsed

# ── Retry-safe followup ───────────────────────────────────────────────────────
async def safe_followup(interaction: discord.Interaction, retries: int = 3, **kwargs):
    for attempt in range(retries):
        try:
            await interaction.followup.send(**kwargs)
            return
        except discord.errors.DiscordServerError as e:
            if attempt < retries - 1:
                wait = 2 ** attempt
                log.warning("Discord 503 (attempt %d/%d), retrying in %ds", attempt + 1, retries, wait)
                await asyncio.sleep(wait)
            else:
                raise
        except discord.errors.NotFound:
            log.warning("Interaction token expired.")
            return

# ── Progress heartbeat ────────────────────────────────────────────────────────
async def progress_heartbeat(interaction: discord.Interaction, stop_event: asyncio.Event):
    spinner = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
    step = elapsed = 0
    while not stop_event.is_set():
        await asyncio.sleep(10)
        if stop_event.is_set():
            break
        elapsed += 10
        try:
            await interaction.edit_original_response(
                content=f"{spinner[step % len(spinner)]} Decompiling... ({elapsed}s elapsed)"
            )
        except Exception:
            pass
        step += 1

# ── Shared decompile handler ──────────────────────────────────────────────────
async def do_decompile(interaction: discord.Interaction, bytecode: bytes, filename: str):
    await interaction.response.defer(thinking=True)

    if len(bytecode) > MAX_FILE_SIZE:
        await safe_followup(interaction, content=f"❌ File too large (max {MAX_FILE_SIZE // 1_000_000} MB).")
        return

    if _decompile_semaphore.locked():
        await interaction.edit_original_response(
            content="⏳ Another decompile is running. You're in the queue..."
        )

    stop_heartbeat = asyncio.Event()

    async with _decompile_semaphore:
        heartbeat = asyncio.create_task(progress_heartbeat(interaction, stop_heartbeat))

        try:
            result, ms = await run_decompile(bytecode)
        except RuntimeError as e:
            stop_heartbeat.set()
            heartbeat.cancel()
            await safe_followup(interaction, embed=discord.Embed(
                title="❌ Decompile Failed",
                description=str(e),
                color=discord.Color.red(),
            ))
            return
        except Exception as e:
            stop_heartbeat.set()
            heartbeat.cancel()
            log.error("Unexpected error: %s", traceback.format_exc())
            await safe_followup(interaction, embed=discord.Embed(
                title="❌ Unexpected Error",
                description=f"```\n{str(e)[:1800]}\n```",
                color=discord.Color.red(),
            ))
            return
        finally:
            stop_heartbeat.set()
            heartbeat.cancel()

    size_in  = len(bytecode)
    size_out = len(result.encode())

    embed = discord.Embed(title="✅ Decompile Complete", color=discord.Color.green())
    embed.add_field(name="Time",       value=f"`{ms:.0f} ms`",            inline=True)
    embed.add_field(name="Input Size", value=f"`{size_in:,} B`",          inline=True)
    embed.add_field(name="Output Size",value=f"`{size_out:,} B`",         inline=True)
    embed.set_footer(text="Powered by luau-lang/luau • discord.gg/FEwEpZFQpN")

    out_name = filename.replace(".luau", "_decompiled.lua").replace(".lua", "_decompiled.lua")
    await interaction.edit_original_response(content=None)
    await safe_followup(
        interaction,
        embed=embed,
        file=discord.File(fp=io.BytesIO(result.encode()), filename=out_name),
    )

# ── Slash commands ────────────────────────────────────────────────────────────
@bot.tree.command(name="decompile", description="Decompile a compiled Luau bytecode file")
@app_commands.describe(file="Upload your compiled .luau bytecode file")
async def cmd_decompile(interaction: discord.Interaction, file: discord.Attachment):
    if not (file.filename.endswith(".luau") or file.filename.endswith(".lua")):
        await interaction.response.send_message(
            "❌ Please upload a `.luau` or `.lua` bytecode file.", ephemeral=True
        )
        return
    if file.size > MAX_FILE_SIZE:
        await interaction.response.send_message(
            f"❌ File too large (max {MAX_FILE_SIZE // 1_000_000} MB).", ephemeral=True
        )
        return

    bytecode = await file.read()
    await do_decompile(interaction, bytecode, file.filename)


@bot.tree.command(name="help", description="How to use the Luau decompiler bot")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔧 Luau Decompiler Bot",
        color=discord.Color.blue(),
        description="Decompile compiled Luau bytecode files back into readable source code.",
    )
    embed.add_field(
        name="📁 `/decompile`",
        value="Upload a compiled `.luau` bytecode file to decompile.",
        inline=False,
    )
    embed.add_field(
        name="⚠️ What files work?",
        value=(
            "This decompiles **compiled Luau bytecode** — the binary output from `luau --compile`.\n"
            "It does **not** work on:\n"
            "• Plain `.lua`/`.luau` source code\n"
            "• `.rbxm`/`.rbxl` Roblox asset files\n"
            "• Scripts obfuscated at the source level"
        ),
        inline=False,
    )
    embed.set_footer(text="Powered by luau-lang/luau • discord.gg/FEwEpZFQpN")
    await interaction.response.send_message(embed=embed)


# ── Keep-alive HTTP server ────────────────────────────────────────────────────
async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)

async def start_keepalive():
    port = int(os.getenv("PORT", "8080"))
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Keep-alive HTTP server listening on port %d", port)

# ── Bot events ────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash command(s).", len(synced))
    except Exception as e:
        log.error("Failed to sync commands: %s", e)
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="your bytecode 🔧")
    )

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        check_luau()
    except RuntimeError as e:
        log.error("Startup check failed: %s", e)
        raise SystemExit(1)

    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        log.error("Set the DISCORD_TOKEN environment variable before running!")
        raise SystemExit(1)

    async def main():
        await start_keepalive()
        try:
            await bot.start(BOT_TOKEN)
        except KeyboardInterrupt:
            pass
        finally:
            if not bot.is_closed():
                await bot.close()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
