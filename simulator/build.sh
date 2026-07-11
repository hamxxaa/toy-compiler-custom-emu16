#!/usr/bin/env bash
# Build the EMU16 core (emu.cpp) to WebAssembly for the browser simulator.
#
# Requires Emscripten on PATH (run `emsdk_env` first). Outputs simulator/emu.js
# + simulator/emu.wasm. Re-run after any ISA change to emu.cpp.
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

emcc emulator/emu.cpp emulator/ppu.cpp emulator/emu_wasm.cpp -O2 \
    -DEMU_COUNT_INSTRUCTIONS \
    -s MODULARIZE=1 \
    -s EXPORT_NAME=createEmu \
    -s "EXPORTED_RUNTIME_METHODS=['cwrap','ccall','HEAPU8']" \
    -s ALLOW_MEMORY_GROWTH=0 \
    -s INITIAL_MEMORY=16777216 \
    -s ENVIRONMENT=web,node \
    -o simulator/emu.js

echo "Built simulator/emu.js + simulator/emu.wasm"
