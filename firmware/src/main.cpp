#include <Arduino.h>
#include <SPI.h>
#include <TFT_eSPI.h>
#include <LittleFS.h>
#include <cstring>
#include <cstdio>
#include "emu.h"
#include "definitions.h"
#include "ppu.h"
#include "hw_pins.h"
#include "storage.h"

TFT_eSPI tft = TFT_eSPI();

uint16_t frame_buffer[SCREEN_WIDTH * SCREEN_HEIGHT];
uint16_t prev_frame_buffer[SCREEN_WIDTH * SCREEN_HEIGHT];  // last frame pushed via the PPU path, to skip redundant pushes
bool     have_prev_frame = false;                          // false forces a push on the first frame / after a ROM swap
uint8_t  prev_vram[VRAM_SIZE];   // last frame's VRAM, to skip redundant pushes
uint8_t  prev_pram[PRAM_SIZE];   // last frame's PRAM, to skip redundant palette rebuilds
uint16_t g_palette[256];         // cached RGB565 palette (rebuilt only when PRAM changes)

// Development toggles.
constexpr bool ENABLE_FRAMEBUFFER_TEST = false; // true = solid-color panel self-test (skips the emulator)
constexpr bool ENABLE_DEBUG_LOGS = true;

uint8_t input_state = 0;

// ---- ROM selector state ----
static constexpr int MAX_ROMS = 16;
static String g_rom_list[MAX_ROMS];
static int g_rom_count = 0;
static String g_pending_rom = "";

void get_input();
void init_default_pram();
void run_framebuffer_color_test();

static void build_rom_list()
{
    g_rom_count = 0;
    // LittleFS must be mounted before we can list it. setup() runs build_rom_list()
    // before load_rom_from_flash(), so mount here (LittleFS.begin() is idempotent).
    if (!LittleFS.begin(true))
    {
        Serial.println("Error: LittleFS mount failed in build_rom_list!");
        return;
    }
    File root = LittleFS.open("/");
    if (!root)
        return;
    File f = root.openNextFile();
    while (f && g_rom_count < MAX_ROMS)
    {
        // f.name() may or may not carry a leading '/' depending on the core version;
        // normalize to exactly one so both listing and loading use a valid path.
        String name = f.name();
        if (!name.startsWith("/"))
            name = String("/") + name;
        if (name.endsWith(".rom"))
            g_rom_list[g_rom_count++] = name;
        f.close();
        f = root.openNextFile();
    }
    root.close();
}

static int write_guest_str(uint16_t dest, const String &s)
{
    int n = s.length();
    for (int i = 0; i < n; ++i)
        cpu_instance.memory[dest + i] = (uint8_t)s[i];
    cpu_instance.memory[dest + n] = 0;
    return n;
}

// ---- per-ROM save namespacing + asset pack (Pieces A/B) ----
static char g_current_rom[40] = {0};   // basename of the loaded ROM, no dir / no ".rom"

struct TocEntry { uint8_t type, w, h; uint32_t offset, length; };
static TocEntry g_toc[64];
static int  g_asset_count = 0;
static char g_pak_path[48] = {0};

// Derive g_current_rom from a "/name.rom" path (strip leading '/' and the ".rom" suffix).
static void set_current_rom(const char *filename)
{
    const char *p = filename;
    if (*p == '/') ++p;
    size_t n = strlen(p);
    if (n >= 4 && strcmp(p + n - 4, ".rom") == 0) n -= 4;
    if (n >= sizeof(g_current_rom)) n = sizeof(g_current_rom) - 1;
    memcpy(g_current_rom, p, n);
    g_current_rom[n] = 0;
}

// Cache the header + TOC of "/<rom>.pak"; blob bytes stay on flash and are streamed on demand.
static void load_asset_toc()
{
    g_asset_count = 0;
    snprintf(g_pak_path, sizeof(g_pak_path), "/%s.pak", g_current_rom);
    File f = storage::open_ro(g_pak_path);
    if (!f) return;
    uint8_t hdr[8];
    if (f.read(hdr, 8) != 8 || memcmp(hdr, "EPAK", 4) != 0) { f.close(); return; }
    int count = hdr[6] | (hdr[7] << 8);
    if (count > 64) count = 64;
    for (int i = 0; i < count; ++i)
    {
        uint8_t e[12];
        if (f.read(e, 12) != 12) break;
        g_toc[i].type   = e[0];
        g_toc[i].w      = e[1];
        g_toc[i].h      = e[2];
        g_toc[i].offset = (uint32_t)e[4] | ((uint32_t)e[5] << 8) | ((uint32_t)e[6] << 16) | ((uint32_t)e[7] << 24);
        g_toc[i].length = (uint32_t)e[8] | ((uint32_t)e[9] << 8) | ((uint32_t)e[10] << 16) | ((uint32_t)e[11] << 24);
        g_asset_count++;
    }
    f.close();
}

// Per-frame pace target in ms, set by SYSCALL_SET_FPS (default 16 ms ~= 60 fps). A lower fps gives
// a longer frame budget (more instructions per frame) and a steadier cadence.
static uint32_t g_frame_target_ms = 16;

static void handle_syscall(uint16_t num)
{
    uint16_t r1 = cpu_instance.registers[1].word;
    uint16_t r2 = cpu_instance.registers[2].word;
    uint16_t r3 = cpu_instance.registers[3].word;
    switch (num)
    {
    case 1: // LIST_ROMS: R1=dest, R2=max -> R0=count; writes len-prefixed names at dest
    {
        int max_roms = r2 ? (int)r2 : MAX_ROMS;
        int count = 0;
        uint16_t cursor = r1;
        for (int i = 0; i < g_rom_count && count < max_roms; ++i, ++count)
        {
            int len = write_guest_str(cursor + 1, g_rom_list[i]);
            cpu_instance.memory[cursor] = (uint8_t)len;
            cursor += 1 + len + 1;
        }
        cpu_instance.registers[0].word = (uint16_t)count;
        break;
    }
    case 2: // GET_ROM_NAME: R1=index, R2=dest -> R0=length
    {
        if (r1 < (uint16_t)g_rom_count)
        {
            int len = write_guest_str(r2, g_rom_list[r1]);
            cpu_instance.registers[0].word = (uint16_t)len;
        }
        else
        {
            cpu_instance.registers[0].word = 0;
        }
        break;
    }
    case 3: // LOAD_ROM: R1=index -> halt CPU, set pending
    {
        if (r1 < (uint16_t)g_rom_count)
            g_pending_rom = g_rom_list[r1];
        cpu_instance.running = false;
        break;
    }
    case 4: // RESET -> reboot to menu
    {
        g_pending_rom = "/menu.rom";
        cpu_instance.running = false;
        break;
    }
    case SYSCALL_TIME: // TIME: R0 = milliseconds since boot (low 16 bits)
        cpu_instance.registers[0].word = (uint16_t)(millis() & 0xFFFF);
        break;
    case SYSCALL_SAVE: // 7: R1=src R2=len R3=slot -> R0 = bytes written (0 = fail)
    {
        char path[56];
        snprintf(path, sizeof(path), "/saves/%s.%u", g_current_rom, r3);
        storage::ensure_dir("/saves");
        uint32_t len = r2;
        if ((uint32_t)r1 + len > 65536u) len = 65536u - r1;
        bool ok = storage::write_file(path, &cpu_instance.memory[r1], len);
        cpu_instance.registers[0].word = ok ? (uint16_t)len : 0;
        break;
    }
    case SYSCALL_LOAD: // 8: R1=dest R2=maxlen R3=slot -> R0 = bytes read (0 = none)
    {
        char path[56];
        snprintf(path, sizeof(path), "/saves/%s.%u", g_current_rom, r3);
        uint32_t maxlen = r2;
        if ((uint32_t)r1 + maxlen > 65536u) maxlen = 65536u - r1;
        cpu_instance.registers[0].word = (uint16_t)storage::read_file(path, &cpu_instance.memory[r1], maxlen);
        break;
    }
    case SYSCALL_SAVE_EXISTS: // 9: R1=slot -> R0 = 1/0
    {
        char path[56];
        snprintf(path, sizeof(path), "/saves/%s.%u", g_current_rom, r1);
        cpu_instance.registers[0].word = storage::exists(path) ? 1 : 0;
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
        cpu_instance.memory[r2 + 4] = (uint8_t)(e.length & 0xFF);
        cpu_instance.memory[r2 + 5] = (uint8_t)((e.length >> 8) & 0xFF);
        cpu_instance.registers[0].word = (uint16_t)e.length;
        break;
    }
    case SYSCALL_ASSET_LOAD: // 11: R1=id R2=dest R3=maxlen -> R0 = bytes copied (0 = fail/too big)
    {
        if ((int)r1 >= g_asset_count) { cpu_instance.registers[0].word = 0; break; }
        TocEntry &e = g_toc[r1];
        if (e.length > r3 || (uint32_t)r2 + e.length > 65536u) { cpu_instance.registers[0].word = 0; break; }
        File f = storage::open_ro(g_pak_path);
        if (!f) { cpu_instance.registers[0].word = 0; break; }
        f.seek(e.offset);
        size_t n = f.read(&cpu_instance.memory[r2], e.length);
        f.close();
        cpu_instance.registers[0].word = (uint16_t)n;
        break;
    }
    case SYSCALL_SET_FPS: // 12: R1=target fps (0=default 60) -> set the frame-pace target
        g_frame_target_ms = r1 ? (1000u / r1) : 16;
        break;
    case SYSCALL_PPU_SUBMIT: // 13: R1=buf R2=len -> feed a PPU command stream; PRESENT yields the frame
    {
        uint32_t len = r2;
        if ((uint32_t)r1 + len > 65536u) len = 65536u - r1;
        if (ppu_receive(&cpu_instance.memory[r1], (int)len))
            emu_request_present();
        break;
    }
    case SYSCALL_PPU_DMA: // 14: R1=pak_id R2=ppu_addr -> R0 = bytes streamed pak(flash)->PPU RAM
    {
        if ((int)r1 >= g_asset_count) { cpu_instance.registers[0].word = 0; break; }
        TocEntry &e = g_toc[r1];
        File f = storage::open_ro(g_pak_path);
        if (!f) { cpu_instance.registers[0].word = 0; break; }
        f.seek(e.offset);
        uint8_t buf[512];
        uint32_t off = 0, total = 0;
        while (off < e.length)
        {
            uint32_t want = e.length - off;
            if (want > sizeof(buf)) want = sizeof(buf);
            size_t got = f.read(buf, want);
            if (got == 0) break;
            total += ppu_write(r2 + off, buf, (uint32_t)got);
            off += got;
        }
        f.close();
        cpu_instance.registers[0].word = (uint16_t)total;
        break;
    }
    case SYSCALL_PPU_UPLOAD: // 15: R1=ppu_addr R2=cpu_src R3=len -> R0 = bytes copied CPU->PPU RAM
    {
        uint32_t len = r3;
        if ((uint32_t)r2 + len > 65536u) len = 65536u - r2;
        cpu_instance.registers[0].word = (uint16_t)ppu_write(r1, &cpu_instance.memory[r2], len);
        break;
    }
    default:
        break;
    }
}

void load_rom_from_flash(const char *filename)
{
    if (ENABLE_DEBUG_LOGS)
    {
        Serial.printf("Game loading: %s\n", filename);
    }

    if (!LittleFS.begin(true))
    {
        Serial.println("Error: Failed to initialize LittleFS!");
        return;
    }

    File rom_file = LittleFS.open(filename, "r");

    if (!rom_file)
    {
        Serial.println("Error: ROM file not found in LittleFS!");
        return;
    }

    size_t file_size = rom_file.size();
    if (ENABLE_DEBUG_LOGS)
    {
        Serial.printf("ROM size: %u bytes\n", static_cast<unsigned int>(file_size));
    }

    if (file_size > static_cast<size_t>(STACK_START_ADDRESS - 1))
    {
        Serial.println("Error: ROM file size exceeds available RAM!");
        rom_file.close();
        return;
    }

    rom_file.read(cpu_instance.memory, file_size);

    rom_file.close();

    // Namespace saves to this ROM and cache its asset-pack TOC (Pieces A/B).
    set_current_rom(filename);
    load_asset_toc();

    if (ENABLE_DEBUG_LOGS)
    {
        Serial.println("Game loaded successfully!");
    }
}

void setup()
{
    Serial.begin(115200);
    delay(3000);

    const int buttons[] = {LEFT_ARROW_PIN, UP_ARROW_PIN, RIGHT_ARROW_PIN, DOWN_ARROW_PIN,
                           BUTTON_A_PIN, BUTTON_B_PIN, BUTTON_X_PIN, BUTTON_Y_PIN};
    for (int i = 0; i < 8; i++)
    {
        pinMode(buttons[i], INPUT_PULLUP);
    }

    tft.init();
    tft.setRotation(3);
    tft.fillScreen(TFT_BLACK);
    tft.setSwapBytes(true);

    if (ENABLE_FRAMEBUFFER_TEST)
    {
        if (ENABLE_DEBUG_LOGS)
        {
            Serial.println("Framebuffer test mode active.");
        }
        run_framebuffer_color_test();
        return;
    }

    initialize_cpu();
    init_default_pram();
    // Font is not host-loaded — it ships inside each ROM (io.lib's font8x8 array) and is written by
    // load_rom_from_flash below. PRAM/VRAM/INPUT live above any ROM image, so they stay host-set.
    build_rom_list();
    if (ENABLE_DEBUG_LOGS)
        Serial.printf("build_rom_list: %d ROM(s) found in LittleFS\n", g_rom_count);
    emu_set_syscall_handler(handle_syscall);
    load_rom_from_flash("/menu.rom");

    for (int i = 0; i < VRAM_SIZE; i++)
    {
        cpu_instance.memory[VRAM_START_ADDRESS + i] = 0;
    }

    // 0xFF (impossible VRAM/PRAM state) forces the first frame to rebuild the palette and push.
    memset(prev_vram, 0xFF, VRAM_SIZE);
    memset(prev_pram, 0xFF, PRAM_SIZE);
}

void loop()
{
    if (ENABLE_FRAMEBUFFER_TEST)
    {
        run_framebuffer_color_test();
        delay(16);
        return;
    }

    uint32_t frame_start_time = millis();
    uint32_t t;

    get_input();
    cpu_instance.memory[INPUT_ADDRESS] = input_state;

    // (1) run a frame of emulation
    t = micros();
    run_frame_instructions();
    uint32_t us_emu = micros() - t;

    if (!g_pending_rom.isEmpty())
    {
        initialize_cpu();
        init_default_pram();
        for (int i = 0; i < VRAM_SIZE; i++)
            cpu_instance.memory[VRAM_START_ADDRESS + i] = 0;
        load_rom_from_flash(g_pending_rom.c_str());
        // 0xFF is an impossible VRAM/PRAM state, so the new ROM's first frame always rebuilds
        // the palette and pushes -- clears the panel off the previous ROM's image. Same idea for
        // the PPU path: force its first frame to push too, so the new ROM's art replaces whatever
        // the previous ROM last drew.
        memset(prev_vram, 0xFF, VRAM_SIZE);
        memset(prev_pram, 0xFF, PRAM_SIZE);
        have_prev_frame = false;
        g_pending_rom = "";
        return;
    }

    // Display path. Once a ROM has engaged the PPU, it owns the frame: convert its composed
    // indexed output through the PPU palette and push it. Otherwise use the legacy VRAM+PRAM path.
    // Both coexist while the PPU reboot is phased in.
    if (ppu_engaged())
    {
        // Games call ppu_present() every loop, so there's no "did the PPU produce a NEW frame"
        // flag to key off (it's true every frame) -- the only real dirty signal is whether the
        // CONVERTED PIXELS actually changed, mirroring the legacy VRAM path's memcmp-before-push
        // below. A push is the expensive part (~8ms via SPI); skipping it on a static frame (menus,
        // a paused dialog, an idle title screen) is a real win. An animating/scrolling game changes
        // every frame and pushes every frame regardless -- same as the VRAM path always did.
        t = micros();
        ppu_convert_rgb565(frame_buffer);
        bool frame_changed = !have_prev_frame ||
            memcmp(frame_buffer, prev_frame_buffer, sizeof(frame_buffer)) != 0;
        if (frame_changed)
        {
            tft.pushImage(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT, frame_buffer);
            memcpy(prev_frame_buffer, frame_buffer, sizeof(frame_buffer));
            have_prev_frame = true;
        }
        uint32_t us_ppu = micros() - t;
        static uint32_t pn = 0, s_ppu = 0, s_pe = 0;
        s_ppu += us_ppu;
        s_pe += us_emu;
        if (++pn >= 60)
        {
            Serial.printf("== PPU avg/frame over %lu frames (us): emulate %lu, convert+compare+push %lu ==\n",
                          pn, s_pe / pn, s_ppu / pn);
            pn = s_ppu = s_pe = 0;
        }
    }
    else
    {
    // (2) palette dirty-check; (3) rebuild the cached palette only if PRAM changed
    t = micros();
    bool pram_changed = memcmp(cpu_instance.memory + PRAM_START_ADDRESS, prev_pram, PRAM_SIZE) != 0;
    uint32_t us_pram_check = micros() - t;

    uint32_t us_pram_build = 0;
    if (pram_changed)
    {
        t = micros();
        for (int i = 0; i < 256; i++)
            g_palette[i] = (uint16_t)(cpu_instance.memory[PRAM_START_ADDRESS + i * 2] | (cpu_instance.memory[PRAM_START_ADDRESS + i * 2 + 1] << 8));
        memcpy(prev_pram, cpu_instance.memory + PRAM_START_ADDRESS, PRAM_SIZE);
        us_pram_build = micros() - t;
    }

    // (4) VRAM dirty-check; (5) rebuild frame buffer + (6) push, only if VRAM changed
    t = micros();
    bool vram_changed = memcmp(cpu_instance.memory + VRAM_START_ADDRESS, prev_vram, VRAM_SIZE) != 0;
    uint32_t us_vram_check = micros() - t;

    uint32_t us_vram_build = 0;
    uint32_t us_push = 0;
    if (vram_changed)
    {
        t = micros();
        for (int i = 0; i < VRAM_SIZE; ++i)
            frame_buffer[i] = g_palette[cpu_instance.memory[VRAM_START_ADDRESS + i]];
        us_vram_build = micros() - t;

        t = micros();
        tft.pushImage(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT, frame_buffer);
        us_push = micros() - t;

        memcpy(prev_vram, cpu_instance.memory + VRAM_START_ADDRESS, VRAM_SIZE);
    }

    // Rolling per-phase averages (microseconds), printed once per ~60 frames.
    static uint32_t n = 0, s_emu = 0, s_pc = 0, s_pb = 0, s_vc = 0, s_vb = 0, s_pu = 0;
    s_emu += us_emu;
    s_pc += us_pram_check;
    s_pb += us_pram_build;
    s_vc += us_vram_check;
    s_vb += us_vram_build;
    s_pu += us_push;
    n++;
    if (n >= 60)
    {
        uint32_t total = (s_emu + s_pc + s_pb + s_vc + s_vb + s_pu) / n;
        Serial.printf("== avg/frame over %lu frames (us) ==\n", n);
        Serial.printf("  emulate    : %lu\n", s_emu / n);
        Serial.printf("  pram check : %lu\n", s_pc / n);
        Serial.printf("  pram build : %lu\n", s_pb / n);
        Serial.printf("  vram check : %lu\n", s_vc / n);
        Serial.printf("  vram build : %lu\n", s_vb / n);
        Serial.printf("  push       : %lu\n", s_pu / n);
        Serial.printf("  TOTAL      : %lu us = %lu.%03lu ms\n", total, total / 1000, total % 1000);
        n = s_emu = s_pc = s_pb = s_vc = s_vb = s_pu = 0;
    }
    } // end legacy VRAM display path

    int32_t frame_duration = millis() - frame_start_time;
    if (frame_duration < (int32_t)g_frame_target_ms)
    {
        delay(g_frame_target_ms - frame_duration);
    }
}

void run_framebuffer_color_test()
{
    // Panel self-test: fill the screen solid green, bypassing the emulator and ROM. If this
    // shows but a ROM stays black, the fault is in the emulator/ROM path, not the display.
    for (int i = 0; i < (SCREEN_WIDTH * SCREEN_HEIGHT); ++i)
        frame_buffer[i] = TFT_GREEN;
    tft.pushImage(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT, frame_buffer);
}

void get_input()
{
    input_state = 0;
    if (digitalRead(LEFT_ARROW_PIN) == LOW)
        input_state |= 0x01;
    if (digitalRead(UP_ARROW_PIN) == LOW)
        input_state |= 0x02;
    if (digitalRead(RIGHT_ARROW_PIN) == LOW)
        input_state |= 0x04;
    if (digitalRead(DOWN_ARROW_PIN) == LOW)
        input_state |= 0x08;
    if (digitalRead(BUTTON_A_PIN) == LOW)
        input_state |= 0x10;
    if (digitalRead(BUTTON_B_PIN) == LOW)
        input_state |= 0x20;
    if (digitalRead(BUTTON_X_PIN) == LOW)
        input_state |= 0x40;
    if (digitalRead(BUTTON_Y_PIN) == LOW)
        input_state |= 0x80;
}

void init_default_pram()
{
    uint16_t defaults[8] = {
        TFT_BLACK, TFT_WHITE, TFT_RED, TFT_BLUE,
        TFT_GREEN, TFT_MAGENTA, TFT_CYAN, TFT_YELLOW};
    for (int i = 0; i < 8; i++)
    {
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2)] = defaults[i] & 0xFF;
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2) + 1] = (defaults[i] >> 8) & 0xFF;
    }
    // Fill the rest of the palette with a grayscale gradient.
    for (int i = 8; i < 256; i++)
    {
        uint16_t gray = tft.color565(i, i, i);
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2)] = gray & 0xFF;
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2) + 1] = (gray >> 8) & 0xFF;
    }
}
