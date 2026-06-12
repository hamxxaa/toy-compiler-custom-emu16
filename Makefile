# Toy Compiler — build, test, and clean targets.
#
# Requirements:
#   pc_emu : g++ / MinGW (any GCC ≥ 7)
#   wasm   : Emscripten (emcc on PATH — run emsdk_env first, or use the workaround in memory/)
#   test   : Python 3, pc_emu already built
#   verify : Node.js, wasm already built
#
# Usage:
#   make           # build pc_emu.exe + WASM
#   make pc_emu    # desktop emulator only
#   make wasm      # WebAssembly only
#   make test      # regression tests (compiles + runs all 23 test ROMs)
#   make verify    # headless WASM cross-check via Node
#   make clean     # remove all build artifacts

CXX      := g++
CXXFLAGS := -std=c++17 -O2 -static

EMU_OUT  := pc_emu.exe
EMU_SRCS := emulator/emu.cpp emulator/pc_emulator_main.cpp
EMU_HDRS := emulator/emu.h emulator/definitions.h

WASM_OUT := simulator/emu.js

.PHONY: all pc_emu wasm test verify flash uploadfs clean

all: pc_emu wasm

# ── Desktop emulator ─────────────────────────────────────────────────────────

pc_emu: $(EMU_OUT)

$(EMU_OUT): $(EMU_SRCS) $(EMU_HDRS)
	$(CXX) $(CXXFLAGS) $(EMU_SRCS) -o $@

# ── WebAssembly simulator ────────────────────────────────────────────────────

wasm: $(WASM_OUT)

$(WASM_OUT): emulator/emu.cpp emulator/emu_wasm.cpp $(EMU_HDRS)
	bash simulator/build.sh

# ── Firmware (ESP32) ─────────────────────────────────────────────────────────

flash:
	cd firmware && pio run -t upload

uploadfs:
	cd firmware && pio run -t uploadfs

# ── Tests ────────────────────────────────────────────────────────────────────

test: $(EMU_OUT)
	python run_tests.py

verify: $(WASM_OUT)
	node verify_wasm.js

# ── Clean ────────────────────────────────────────────────────────────────────

clean:
	rm -f $(EMU_OUT) $(WASM_OUT) simulator/emu.wasm
	rm -rf build/roms build/pc_emulator firmware/.pio
