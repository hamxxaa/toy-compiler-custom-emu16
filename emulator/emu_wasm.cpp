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
#include <cstring>
#include <cstdio>
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
static int  g_target_fps = 60;     // SYSCALL_SET_FPS target; JS reads emu_target_fps() to pace RAF

static uint8_t wasm_name_len(const char *s)
{
    uint8_t n = 0;
    while (n < 63 && s[n]) ++n;
    return n;
}

// ---- save/load + asset state (Pieces A/B) ----
// Saves go to localStorage keyed by "emu16:<rom>.<slot>"; the asset pack is fetched by JS into a
// heap buffer (emu_set_asset_pack) and streamed from there. Guards make the KV calls no-ops under
// node (verify_wasm.js) where localStorage doesn't exist.
static char g_wasm_current_rom[64] = {0};

struct TocEntry { uint8_t type, w, h; uint32_t offset, length; };
static uint8_t *g_pak = nullptr;
static int g_pak_size = 0;
static uint8_t g_pak_storage[262144];   // browser-resident pack (the device streams from flash instead)
static TocEntry g_toc[64];
static int g_asset_count = 0;

static void parse_asset_toc()
{
    g_asset_count = 0;
    if (!g_pak || g_pak_size < 8 || std::memcmp(g_pak, "EPAK", 4) != 0) return;
    int count = g_pak[6] | (g_pak[7] << 8);
    if (count > 64) count = 64;
    if (g_pak_size < 8 + 12 * count) return;
    for (int i = 0; i < count; ++i)
    {
        const uint8_t *e = g_pak + 8 + 12 * i;
        g_toc[i].type   = e[0];
        g_toc[i].w      = e[1];
        g_toc[i].h      = e[2];
        g_toc[i].offset = (uint32_t)e[4] | ((uint32_t)e[5] << 8) | ((uint32_t)e[6] << 16) | ((uint32_t)e[7] << 24);
        g_toc[i].length = (uint32_t)e[8] | ((uint32_t)e[9] << 8) | ((uint32_t)e[10] << 16) | ((uint32_t)e[11] << 24);
        ++g_asset_count;
    }
}

EM_JS(int, js_kv_save, (const char *key, const uint8_t *p, int n), {
    if (typeof localStorage === 'undefined') return 0;
    var k = ""; for (var i = 0; HEAPU8[key + i]; i++) k += String.fromCharCode(HEAPU8[key + i]);
    var b = ""; for (var j = 0; j < n; j++) b += String.fromCharCode(HEAPU8[p + j]);
    try { localStorage.setItem('emu16:' + k, btoa(b)); return n; } catch (e) { return 0; }
});

EM_JS(int, js_kv_load, (const char *key, uint8_t *p, int max), {
    if (typeof localStorage === 'undefined') return 0;
    var k = ""; for (var i = 0; HEAPU8[key + i]; i++) k += String.fromCharCode(HEAPU8[key + i]);
    var s = localStorage.getItem('emu16:' + k);
    if (s === null) return 0;
    var b = atob(s); var n = Math.min(b.length, max);
    for (var j = 0; j < n; j++) HEAPU8[p + j] = b.charCodeAt(j) & 0xFF;
    return n;
});

EM_JS(int, js_kv_has, (const char *key), {
    if (typeof localStorage === 'undefined') return 0;
    var k = ""; for (var i = 0; HEAPU8[key + i]; i++) k += String.fromCharCode(HEAPU8[key + i]);
    return localStorage.getItem('emu16:' + k) !== null ? 1 : 0;
});

static void wasm_syscall_handler(uint16_t num)
{
    uint16_t r1 = cpu_instance.registers[1].word;
    uint16_t r2 = cpu_instance.registers[2].word;
    uint16_t r3 = cpu_instance.registers[3].word;
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
    case SYSCALL_SAVE: // 7: R1=src R2=len R3=slot -> R0 = bytes written
    {
        char key[80];
        snprintf(key, sizeof(key), "%s.%u", g_wasm_current_rom, r3);
        uint32_t len = r2;
        if ((uint32_t)r1 + len > 65536u) len = 65536u - r1;
        cpu_instance.registers[0].word = static_cast<uint16_t>(js_kv_save(key, &cpu_instance.memory[r1], len));
        break;
    }
    case SYSCALL_LOAD: // 8: R1=dest R2=maxlen R3=slot -> R0 = bytes read
    {
        char key[80];
        snprintf(key, sizeof(key), "%s.%u", g_wasm_current_rom, r3);
        uint32_t maxlen = r2;
        if ((uint32_t)r1 + maxlen > 65536u) maxlen = 65536u - r1;
        cpu_instance.registers[0].word = static_cast<uint16_t>(js_kv_load(key, &cpu_instance.memory[r1], maxlen));
        break;
    }
    case SYSCALL_SAVE_EXISTS: // 9: R1=slot -> R0 = 1/0
    {
        char key[80];
        snprintf(key, sizeof(key), "%s.%u", g_wasm_current_rom, r1);
        cpu_instance.registers[0].word = static_cast<uint16_t>(js_kv_has(key));
        break;
    }
    case SYSCALL_ASSET_INFO: // 10: R1=id R2=dest -> R0=length; writes 6-byte header at dest
    {
        if ((int)r1 >= g_asset_count) { cpu_instance.registers[0].word = 0; break; }
        TocEntry &e = g_toc[r1];
        cpu_instance.memory[r2 + 0] = e.type;
        cpu_instance.memory[r2 + 1] = e.w;
        cpu_instance.memory[r2 + 2] = e.h;
        cpu_instance.memory[r2 + 3] = 0;
        cpu_instance.memory[r2 + 4] = static_cast<uint8_t>(e.length & 0xFF);
        cpu_instance.memory[r2 + 5] = static_cast<uint8_t>((e.length >> 8) & 0xFF);
        cpu_instance.registers[0].word = static_cast<uint16_t>(e.length);
        break;
    }
    case SYSCALL_ASSET_LOAD: // 11: R1=id R2=dest R3=maxlen -> R0 = bytes copied (0 = fail/too big)
    {
        if ((int)r1 >= g_asset_count) { cpu_instance.registers[0].word = 0; break; }
        TocEntry &e = g_toc[r1];
        if (e.length > r3 || (uint32_t)r2 + e.length > 65536u ||
            (int)(e.offset + e.length) > g_pak_size)
        {
            cpu_instance.registers[0].word = 0;
            break;
        }
        std::memcpy(&cpu_instance.memory[r2], g_pak + e.offset, e.length);
        cpu_instance.registers[0].word = static_cast<uint16_t>(e.length);
        break;
    }
    case SYSCALL_SET_FPS: // 12: store the target; the JS RAF loop reads emu_target_fps() to pace
        g_target_fps = r1 ? static_cast<int>(r1) : 60;
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
EMSCRIPTEN_KEEPALIVE int   emu_target_fps()         { return g_target_fps; }

// ---- save/asset bridges (Pieces A/B) ----
// On each ROM load JS sets the current name (for save namespacing) and, if the game has a pack,
// copies the pack bytes into emu_asset_buf() (max emu_asset_capacity()) then calls
// emu_commit_asset_pack(n). A fixed buffer avoids depending on _malloc being exported.
EMSCRIPTEN_KEEPALIVE void emu_set_current_rom(const char *name)
{
    std::strncpy(g_wasm_current_rom, name, sizeof(g_wasm_current_rom) - 1);
    g_wasm_current_rom[sizeof(g_wasm_current_rom) - 1] = 0;
}
EMSCRIPTEN_KEEPALIVE uint8_t *emu_asset_buf()      { return g_pak_storage; }
EMSCRIPTEN_KEEPALIVE int      emu_asset_capacity() { return (int)sizeof(g_pak_storage); }
EMSCRIPTEN_KEEPALIVE void emu_commit_asset_pack(int n)
{
    if (n < 0) n = 0;
    if (n > (int)sizeof(g_pak_storage)) n = (int)sizeof(g_pak_storage);
    g_pak = g_pak_storage;
    g_pak_size = n;
    parse_asset_toc();
}

} // extern "C"
