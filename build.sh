#!/usr/bin/env bash
# build.sh — Render build script for Luau Decompiler Bot
# Builds the official Roblox luau CLI from source (includes decompiler)
set -euo pipefail

VENDOR_DIR="$(pwd)/vendor"
mkdir -p "$VENDOR_DIR/bin"

# ── 1. Build luau from source ─────────────────────────────────────────────────
echo "=== [1/3] Building luau CLI from source ==="

# Check if cmake is available
cmake --version || { echo "cmake not found, trying to install..."; apt-get install -y cmake 2>/dev/null || true; }

LUAU_DIR="$(pwd)/luau-src"
if [ -d "$LUAU_DIR" ]; then
    echo "→ Updating existing luau clone..."
    git -C "$LUAU_DIR" pull --ff-only
else
    echo "→ Cloning luau..."
    git clone --depth=1 https://github.com/luau-lang/luau.git "$LUAU_DIR"
fi

cd "$LUAU_DIR"
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release 2>&1 | tail -5
make -j"$(nproc)" luau 2>&1 | tail -10
cp luau "$VENDOR_DIR/bin/luau"
cd ../..
rm -rf "$LUAU_DIR"

"$VENDOR_DIR/bin/luau" --version
echo "luau built ✓ → $VENDOR_DIR/bin/luau"

# ── 2. Python dependencies ────────────────────────────────────────────────────
echo ""
echo "=== [2/3] Installing Python dependencies ==="
pip install --upgrade pip --quiet
pip install -r requirements.txt

echo ""
echo "=== [3/3] Done ==="
echo "luau binary : $VENDOR_DIR/bin/luau"
echo "=== Build complete ✅ ==="
