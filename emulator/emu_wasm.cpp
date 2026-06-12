// WebAssembly entry points for the EMU16 core.
//
// This wraps the *same* emu.cpp used by pc_emu.exe and the ESP32 firmware, so the
// browser simulator runs the real ISA — no hand-ported JS interpreter to drift.
//
// Build (see simulator/build.sh):
//   emcc emulator/emu.cpp emulator/emu_wasm.cpp -O2 -o simulator/emu.js \
//        -s MODULARIZE -s EXPORT_NAME=createEmu -s EXPORTED_RUNTIME_METHODS=['ccall','cwrap'] ...
//
// JS-side flow: emu_init() → write ROM bytes into the heap at emu_mem() → emu_run_frame()
// → read VRAM/registers back through the same heap pointer.

#include <cstdint>
#include <emscripten/emscripten.h>

#include "emu.h"
#include "definitions.h"

// Defined in emu.cpp (not exposed via emu.h, but non-static globals).
uint16_t read_word_le(uint16_t address);
void decode_and_execute(uint16_t instruction);

static uint16_t rgb565_from_rgb(uint8_t red, uint8_t green, uint8_t blue)
{
    return static_cast<uint16_t>(((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3));
}

// Mirrors init_default_pram() in pc_emulator_main.cpp: 8 named colors + a grayscale ramp.
static void init_default_pram()
{
    const uint16_t defaults[8] = {
        0x0000, 0xFFFF, 0xF800, 0x001F,
        0x07E0, 0xF81F, 0x07FF, 0xFFE0
    };

    for (int i = 0; i < 8; ++i)
    {
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2)]     = static_cast<uint8_t>(defaults[i] & 0xFF);
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2) + 1] = static_cast<uint8_t>((defaults[i] >> 8) & 0xFF);
    }

    for (int i = 8; i < 256; ++i)
    {
        uint16_t gray = rgb565_from_rgb(static_cast<uint8_t>(i), static_cast<uint8_t>(i), static_cast<uint8_t>(i));
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2)]     = static_cast<uint8_t>(gray & 0xFF);
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2) + 1] = static_cast<uint8_t>((gray >> 8) & 0xFF);
    }
}

extern "C" {

// Reset CPU + memory and lay down the default palette / cleared VRAM / zero input.
// Call this, THEN write the ROM bytes into the heap (initialize_cpu zeroes all memory).
EMSCRIPTEN_KEEPALIVE
void emu_init()
{
    initialize_cpu();                 // zeroes all 64 KB, resets regs/pc/flags, SP = STACK_START_ADDRESS
    init_default_pram();
    for (int i = 0; i < VRAM_SIZE; ++i)
        cpu_instance.memory[VRAM_START_ADDRESS + i] = 0;
    cpu_instance.memory[INPUT_ADDRESS] = 0;
}

// Pointer to the 64 KB guest memory inside the WASM heap. JS reads/writes it directly
// (ROM load, INPUT writes, VRAM/PRAM readback) via Module.HEAPU8.
EMSCRIPTEN_KEEPALIVE
uint8_t *emu_mem()
{
    return cpu_instance.memory;
}

// Run up to `max_instructions` (the speed slider feeds this). Returns 1 if still running, 0 if halted.
EMSCRIPTEN_KEEPALIVE
int emu_run_frame(int max_instructions)
{
    int n = 0;
    while (cpu_instance.running && n < max_instructions)
    {
        uint16_t instruction = read_word_le(cpu_instance.pc);
        decode_and_execute(instruction);
        ++n;
    }
    return cpu_instance.running ? 1 : 0;
}

EMSCRIPTEN_KEEPALIVE int      emu_reg(int i)   { return cpu_instance.registers[i & 7].word; }
EMSCRIPTEN_KEEPALIVE int      emu_pc()         { return cpu_instance.pc; }
EMSCRIPTEN_KEEPALIVE int      emu_flags()      { return cpu_instance.flags; }
EMSCRIPTEN_KEEPALIVE int      emu_running()    { return cpu_instance.running ? 1 : 0; }

} // extern "C"
