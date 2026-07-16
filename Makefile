# Toy Compiler — build, test, and clean targets. Runs under Git Bash on Windows (or any Unix
# shell); recipes use POSIX tools (g++, emcc, python, node, rm).
#
# Requirements:
#   pc_emu : g++ / MinGW (any GCC >= 7) on PATH
#   wasm   : Emscripten (emcc on PATH) — Windows: run `emsdk_env` first; the recipe calls emcc
#            directly (no bash), so it works from cmd/PowerShell once emsdk is activated.
#   test   : Python 3 on PATH, pc_emu already built
#   verify : Node.js on PATH (emsdk bundles one — activate emsdk), wasm already built
#   flash / uploadfs : PlatformIO (pio) on PATH, or installed in the default user location
#
# Usage (Windows: mingw32-make <target>; Unix: make <target>):
#   make           # build pc_emu + WASM
#   make pc_emu    # desktop emulator only
#   make wasm      # WebAssembly only        (needs emcc on PATH)
#   make test      # regression tests
#   make verify    # headless WASM cross-check via Node   (needs node on PATH)
#   make clean     # remove all build artifacts

CXX      := g++
CXXFLAGS := -std=c++17 -O2 -static

EMU_OUT  := pc_emu.exe
EMU_SRCS := emulator/emu.cpp emulator/ppu.cpp emulator/apu.cpp emulator/pc_emulator_main.cpp
EMU_HDRS := emulator/emu.h emulator/definitions.h emulator/ppu.h emulator/apu.h

WASM_OUT  := simulator/emu.js
EMCC      := emcc
# Keep these flags in sync with simulator/build.sh (the manual bash build).
# -DEMU_COUNT_INSTRUCTIONS: the simulator is a dev tool -> enable the executed-instruction counter so
# the browser can show live instructions/second + instructions/frame (negligible per-instruction cost;
# firmware still omits it for zero production overhead).
EMCC_FLAGS := -O2 -DEMU_COUNT_INSTRUCTIONS -s MODULARIZE=1 -s EXPORT_NAME=createEmu \
              -s "EXPORTED_RUNTIME_METHODS=['cwrap','ccall','HEAPU8','HEAP16']" \
              -s ALLOW_MEMORY_GROWTH=0 -s INITIAL_MEMORY=16777216 -s ENVIRONMENT=web,node

PIO ?= pio
ifeq ($(OS),Windows_NT)
PIO_HOME := $(subst \,/,$(HOME))
PIO_USERPROFILE := $(subst \,/,$(USERPROFILE))
ifneq ($(wildcard $(PIO_HOME)/.platformio/penv/Scripts/pio.exe),)
PIO := $(PIO_HOME)/.platformio/penv/Scripts/pio.exe
else ifneq ($(wildcard $(PIO_USERPROFILE)/.platformio/penv/Scripts/pio.exe),)
PIO := $(PIO_USERPROFILE)/.platformio/penv/Scripts/pio.exe
endif
endif

# ── Clean shims ──────────────────────────────────────────────────────────────
# POSIX rm (Git Bash on Windows, or any Unix shell). rm -f / -rf tolerate missing paths.
RM_FILES = rm -f $(EMU_OUT) $(SONGREND_OUT) $(WASM_OUT) simulator/emu.wasm
RM_BUILD = rm -rf build/roms
RM_PCEMU = rm -rf build/pc_emulator
RM_PIO   = rm -rf firmware/.pio

.PHONY: all pc_emu song_render wasm test verify flash uploadfs monitor flash-cpu flash-ppu mon-cpu mon-ppu clean clean-roms

all: pc_emu wasm

# ── Desktop emulator ─────────────────────────────────────────────────────────

pc_emu: $(EMU_OUT)

# -DEMU_COUNT_INSTRUCTIONS enables the executed-instruction counter for metrics. It is set ONLY
# for the desktop dev/metrics build; firmware (pio) and WASM omit it -> zero production overhead.
$(EMU_OUT): $(EMU_SRCS) $(EMU_HDRS)
	$(CXX) $(CXXFLAGS) -DEMU_COUNT_INSTRUCTIONS $(EMU_SRCS) -o $@

# ── Music preview renderer (.song -> .wav, no ROM) ───────────────────────────
# Fast write->hear loop for MML music: `tools/mml.py song.mml --preview` uses this under the hood.
SONGREND_OUT := song_render.exe
song_render: $(SONGREND_OUT)
$(SONGREND_OUT): emulator/apu.cpp emulator/apu.h emulator/song_render.cpp
	$(CXX) $(CXXFLAGS) emulator/apu.cpp emulator/song_render.cpp -o $@

# ── WebAssembly simulator ────────────────────────────────────────────────────
# Calls emcc directly (no bash) so it runs from cmd/PowerShell too. emcc must be on PATH
# (Windows: run emsdk_env first). emcc accepts forward-slash paths on every platform.

wasm: $(WASM_OUT)

$(WASM_OUT): emulator/emu.cpp emulator/ppu.cpp emulator/apu.cpp emulator/emu_wasm.cpp $(EMU_HDRS)
	$(EMCC) emulator/emu.cpp emulator/ppu.cpp emulator/apu.cpp emulator/emu_wasm.cpp $(EMCC_FLAGS) -o $(WASM_OUT)

# ── Firmware (ESP32) ─────────────────────────────────────────────────────────
# `cd x && y` works in both cmd.exe and sh. Three build configs (firmware/platformio.ini):
#   single (default) = one-board firmware · cpu / ppu = the two-chip roles.
# flash/uploadfs/monitor default to `single`; the two-chip role targets are below.

flash:
	cd firmware && $(PIO) run -t upload

uploadfs:
	cd firmware && $(PIO) run -t uploadfs

monitor:
	cd firmware && $(PIO) device monitor -b 115200

# Two-chip roles. Connect ONE board at a time (or pass UPLOAD_PORT=COMx / MON_PORT=COMx).
# CPU chip = the clone; PPU chip = the original ESP32-S3.
flash-cpu:
	cd firmware && $(PIO) run -e cpu -t upload $(if $(UPLOAD_PORT),--upload-port $(UPLOAD_PORT))

flash-ppu:
	cd firmware && $(PIO) run -e ppu -t upload $(if $(UPLOAD_PORT),--upload-port $(UPLOAD_PORT))

mon-cpu:
	cd firmware && $(PIO) device monitor -e cpu -b 115200 $(if $(MON_PORT),-p $(MON_PORT))

mon-ppu:
	cd firmware && $(PIO) device monitor -e ppu -b 115200 $(if $(MON_PORT),-p $(MON_PORT))

# ── Tests ────────────────────────────────────────────────────────────────────

test: $(EMU_OUT)
	python run_tests.py

verify: $(WASM_OUT)
	node verify_wasm.js

# ── Clean ────────────────────────────────────────────────────────────────────

# `clean` deliberately KEEPS build/roms (compiled ROMs + .pak asset packs): regenerating assets is a
# multi-step pipeline (art -> image_import -> pack_assets), so wiping them on every clean is painful.
# Use `make clean-roms` to remove them explicitly.
clean:
	$(RM_FILES)
	$(RM_PCEMU)
	$(RM_PIO)

clean-roms:
	$(RM_BUILD)
