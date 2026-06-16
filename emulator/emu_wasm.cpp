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

// Browser syscall handler. Mirrors the desktop handler (pc_emulator_main.cpp): it serves the ROM
// names the JS frontend feeds in (LIST/GET) and signals which ROM to load via g_pending (JS holds
// the bytes and does the actual reload, like the firmware's g_pending_rom). See wasm.js / app.js.
static char g_rom_names[16][64];   // NUL-terminated names JS writes in (64-byte stride)
static int  g_rom_count = 0;       // how many of g_rom_names are valid
static int  g_pending   = -1;      // -1 none | >=0 load that game index | -2 reset -> menu

static uint8_t wasm_name_len(const char *s)
{
    uint8_t n = 0;
    while (n < 63 && s[n]) ++n;
    return n;
}

static void wasm_syscall_handler(uint16_t num)
{
    uint16_t r1 = cpu_instance.registers[1].word;
    uint16_t r2 = cpu_instance.registers[2].word;
    switch (num)
    {
    case 1: // LIST_ROMS: R1=dest, R2=max -> R0=count; writes [len][name][NUL] records
    {
        int max_roms = r2 ? static_cast<int>(r2) : g_rom_count;
        int count = 0;
        uint16_t cursor = r1;
        for (int i = 0; i < g_rom_count && count < max_roms; ++i, ++count)
        {
            uint8_t len = wasm_name_len(g_rom_names[i]);
            cpu_instance.memory[cursor] = len;
            for (int k = 0; k < len; ++k)
                cpu_instance.memory[cursor + 1 + k] = static_cast<uint8_t>(g_rom_names[i][k]);
            cpu_instance.memory[cursor + 1 + len] = 0;
            cursor += static_cast<uint16_t>(1 + len + 1);
        }
        cpu_instance.registers[0].word = static_cast<uint16_t>(count);
        break;
    }
    case 2: // GET_ROM_NAME: R1=index, R2=dest -> R0=length
    {
        if (r1 < static_cast<uint16_t>(g_rom_count))
        {
            uint8_t len = wasm_name_len(g_rom_names[r1]);
            for (int k = 0; k < len; ++k)
                cpu_instance.memory[r2 + k] = static_cast<uint8_t>(g_rom_names[r1][k]);
            cpu_instance.memory[r2 + len] = 0;
            cpu_instance.registers[0].word = len;
        }
        else
        {
            cpu_instance.registers[0].word = 0;
        }
        break;
    }
    case 3: // LOAD_ROM: R1=index
        g_pending = static_cast<int>(r1);
        cpu_instance.running = false;
        break;
    case 4: // RESET -> menu
        g_pending = -2;
        cpu_instance.running = false;
        break;
    case SYSCALL_TIME: // TIME: R0 = milliseconds since page load (low 16 bits)
        cpu_instance.registers[0].word =
            static_cast<uint16_t>(static_cast<uint32_t>(emscripten_get_now()) & 0xFFFF);
        break;
    case 0x7F: // ECHO (test parity with the desktop dev handler)
        cpu_instance.registers[0].word = static_cast<uint16_t>(r1 + r2);
        break;
    default:
        break;
    }
}

extern "C" {

// Reset CPU + memory and lay down the default palette / cleared VRAM / zero input.
// Call this, THEN write the ROM bytes into the heap (initialize_cpu zeroes all memory).
EMSCRIPTEN_KEEPALIVE
void emu_init()
{
    initialize_cpu();                 // zeroes all 64 KB, resets regs/pc/flags, SP = STACK_START_ADDRESS
    emu_set_syscall_handler(wasm_syscall_handler);
    init_default_pram();
    // Font is not host-loaded — it ships inside the ROM (io.lib's font8x8 array), written into the
    // heap by JS after this call. PRAM/VRAM/INPUT live above any ROM image, so they stay host-set.
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
    emu_begin_frame();   // resume if the previous frame ended on a PRESENT yield
    int n = 0;
    while (cpu_instance.running && n < max_instructions)
    {
        uint16_t instruction = read_word_le(cpu_instance.pc);
        decode_and_execute(instruction);
        ++n;
    }
    // A PRESENT yield isn't a halt -> report "still running" so the JS loop keeps going (and paces it).
    return (cpu_instance.running || emu_present_pending()) ? 1 : 0;
}

EMSCRIPTEN_KEEPALIVE int      emu_reg(int i)   { return cpu_instance.registers[i & 7].word; }
EMSCRIPTEN_KEEPALIVE int      emu_pc()         { return cpu_instance.pc; }
EMSCRIPTEN_KEEPALIVE int      emu_flags()      { return cpu_instance.flags; }
EMSCRIPTEN_KEEPALIVE int      emu_running()    { return cpu_instance.running ? 1 : 0; }

// ---- ROM-library bridge for the syscall handler ----
// JS writes up to 16 NUL-terminated names into emu_rom_names() (64-byte stride) and calls
// emu_set_rom_count(); after a halt it polls emu_pending_rom() to hot-swap the next ROM.
EMSCRIPTEN_KEEPALIVE char *emu_rom_names()         { return &g_rom_names[0][0]; }
EMSCRIPTEN_KEEPALIVE void  emu_set_rom_count(int n) { g_rom_count = (n < 16) ? n : 16; }
EMSCRIPTEN_KEEPALIVE int   emu_pending_rom()        { int p = g_pending; g_pending = -1; return p; }

} // extern "C"
