#include <Arduino.h>
#include <SPI.h>
#include <TFT_eSPI.h>
#include <LittleFS.h>
#include <driver/i2s.h>
#include <freertos/message_buffer.h>
#include <cstring>
#include <cstdio>
#include "emu.h"
#include "definitions.h"
#include "ppu.h"
#include "apu.h"
#include "hw_pins.h"
#include "storage.h"
#include "wire.h"        // inter-chip SPI link (two-chip builds)
#include "selftest.h"    // on-device bring-up diagnostics (DIAG)

TFT_eSPI tft = TFT_eSPI();

uint16_t frame_buffer[SCREEN_WIDTH * SCREEN_HEIGHT];
uint16_t prev_frame_buffer[SCREEN_WIDTH * SCREEN_HEIGHT];  // last frame pushed, to skip redundant pushes
bool     have_prev_frame = false;                          // false forces a push on the first frame / after a ROM swap

constexpr bool ENABLE_AUDIO      = true;   // false = no I2S, no audio task (the pre-audio firmware)
constexpr bool ENABLE_DEBUG_LOGS = true;

// Audio config. AUDIO_RATE must equal APU_RATE — the audio task feeds apu_render()'s output straight
// to I2S with no resampling.
constexpr int AUDIO_RATE  = APU_RATE;   // 22050 Hz
constexpr int AUDIO_BLOCK = 256;        // frames per apu_render / i2s_write / DMA buffer
static_assert(AUDIO_RATE == APU_RATE, "I2S rate must match the APU's native rate (no resampler here)");

// Command transport, game core (1) -> audio core (0). The APU commands are an immutable byte batch,
// so the game core ships the bytes across and the audio task calls apu_receive() itself: the APU
// state is touched by one core only, so no lock. MessageBuffer sends are atomic — a batch that
// doesn't fit is dropped whole, never truncated (a truncated command stream would decode as garbage).
static constexpr int APU_CMD_MAX   = 2048;   // apu_receive() clamps to this
static constexpr int APU_CMD_QUEUE = 4096;   // one instrument upload + several frame batches
static MessageBufferHandle_t audio_cmd_buf     = NULL;
static TaskHandle_t          audio_task_handle = NULL;
static uint8_t               audio_cmd_scratch[APU_CMD_MAX];
static volatile uint32_t     g_apu_dropped   = 0;   // batches dropped (queue full), a diagnostic count
static volatile bool         g_apu_flush_req = false; // ask the audio task to discard queued commands

uint8_t input_state = 0;

// ---- ROM selector state ----
static constexpr int MAX_ROMS = 16;
static String g_rom_list[MAX_ROMS];
static int g_rom_count = 0;
static String g_pending_rom = "";

void get_input();
void audio_i2s_begin();
void audio_task_begin();
#if defined(WIRE_ROLE_PPU)
static void display_setup();   // the PPU chip: receive commands over the wire, compose, push to the TFT
static void display_loop();
#else
static void emu_host_setup();  // single + cpu builds: run the emulator (ppu_sink routes the display)
static void emu_host_loop();
#endif

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

// ── PPU sink ─────────────────────────────────────────────────────────────────
// The three PPU syscalls go through this instead of calling the PPU (or the wire) directly. A single
// build drives the local ppu.cpp; a cpu build ships the same bytes to the PPU chip over SPI.
#if defined(WIRE_ROLE_CPU)
static uint8_t s_ppu_wr[WIRE_MAX_MSG];   // scratch to assemble a MSG_PPU_WRITE: [addr:2][data...]

static bool ppu_sink_submit(const uint8_t *buf, int len)
{
    wire_send(MSG_PPU_CMD, buf, (uint16_t)len);
    // A game frame always ends its command stream in PRESENT (ppu_present is the only flush path), so
    // a SUBMIT is always a frame boundary -> yield so the loop paces + the guest advances. (No current
    // ROM submits mid-frame without PRESENT; if one ever did, this would over-yield harmlessly.)
    return true;
}
static uint32_t ppu_sink_write(uint16_t addr, const uint8_t *src, uint32_t len)
{
    uint32_t off = 0;
    while (off < len) {
        uint32_t chunk = len - off;
        if (chunk > (uint32_t)WIRE_MAX_MSG - 6) chunk = WIRE_MAX_MSG - 6;   // 4 header + 2 addr
        uint16_t a = (uint16_t)(addr + off);
        s_ppu_wr[0] = a & 0xFF;
        s_ppu_wr[1] = (a >> 8) & 0xFF;
        memcpy(s_ppu_wr + 2, src + off, chunk);
        wire_send(MSG_PPU_WRITE, s_ppu_wr, (uint16_t)(2 + chunk));
        off += chunk;
    }
    return len;
}
static void ppu_sink_reset() { wire_send(MSG_RESET, nullptr, 0); }
#else
static inline bool     ppu_sink_submit(const uint8_t *buf, int len)                    { return ppu_receive(buf, len); }
static inline uint32_t ppu_sink_write(uint16_t addr, const uint8_t *src, uint32_t len) { return ppu_write(addr, src, len); }
static inline void     ppu_sink_reset()                                                { ppu_reset(); }
#endif

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
        if (ppu_sink_submit(&cpu_instance.memory[r1], (int)len))
            emu_request_present();
        break;
    }
    case SYSCALL_APU_SUBMIT: // 16: R1=buf R2=len -> queue an APU command stream (no PRESENT-style latch)
    {
        uint32_t len = r2;
        if ((uint32_t)r1 + len > 65536u) len = 65536u - r1;   // never read past guest memory
        if (len > (uint32_t)APU_CMD_MAX) len = APU_CMD_MAX;
        // Deliberately NOT apu_receive() here: the APU belongs to the audio task on the other core.
        // Queue the bytes and return -- this must never block the game loop for audio's sake.
        if (len && audio_cmd_buf)
        {
            if (xMessageBufferSend(audio_cmd_buf, &cpu_instance.memory[r1], len, 0) == 0)
                g_apu_dropped++;   // queue full: drop this batch WHOLE (see the transport note above)
        }
        break;
    }
    case SYSCALL_APU_TICKS: // 17: R0 = the APU's 60 Hz frame-tick counter (low 16 bits)
    {
        // Counted on the audio core (inside apu_render), read here on the game core. A plain 32-bit
        // aligned load is atomic on this CPU, so this needs no lock -- and the value is only ever
        // used as a DIFFERENCE, so reading a tick late is self-correcting on the next call.
        // This is what unties the song's tempo from the game's frame rate: the counter advances at
        // true 60 Hz off the I2S clock whether the game manages 60 fps or 44.
        cpu_instance.registers[0].word = (uint16_t)(apu_ticks() & 0xFFFF);
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
            total += ppu_sink_write(r2 + off, buf, (uint32_t)got);
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
        cpu_instance.registers[0].word = (uint16_t)ppu_sink_write(r1, &cpu_instance.memory[r2], len);
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
    if (selftest_setup()) return;   // a diagnostic (selftest.h DIAG) took over
#if defined(WIRE_ROLE_PPU)
    display_setup();
#else
    emu_host_setup();
#endif
}

#if !defined(WIRE_ROLE_PPU)
static void emu_host_setup()
{
    const int buttons[] = {LEFT_ARROW_PIN, UP_ARROW_PIN, RIGHT_ARROW_PIN, DOWN_ARROW_PIN,
                           BUTTON_A_PIN, BUTTON_B_PIN, BUTTON_X_PIN, BUTTON_Y_PIN};
    for (int i = 0; i < 8; i++)
        pinMode(buttons[i], INPUT_PULLUP);

    // Init audio I2S before the SPI bus, so I2S claims its GDMA channel without contention.
    if (ENABLE_AUDIO)
        audio_i2s_begin();

#if defined(WIRE_ROLE_CPU)
    wire_master_begin();   // no local TFT — the display is the PPU chip over SPI
#else
    tft.init();
    tft.setRotation(3);
    tft.fillScreen(TFT_BLACK);
    tft.setSwapBytes(true);
#endif

    initialize_cpu();   // resets CPU, PPU, and APU
    // The font is not host-loaded — it ships inside each ROM (io.lib's font8x8 array), written by
    // load_rom_from_flash below.
    build_rom_list();
    if (ENABLE_DEBUG_LOGS)
        Serial.printf("build_rom_list: %d ROM(s) found in LittleFS\n", g_rom_count);
    emu_set_syscall_handler(handle_syscall);
#if defined(WIRE_ROLE_CPU)
    ppu_sink_reset();   // clear the PPU chip before the first ROM draws to it
#endif
    load_rom_from_flash("/menu.rom");

    // Start the audio task last: everything above wrote APU state on this core, and the task reads it
    // on the other core, so there's no boot-time race.
    if (ENABLE_AUDIO)
        audio_task_begin();
}
#endif  // !WIRE_ROLE_PPU

void loop()
{
    if (selftest_loop()) return;   // a diagnostic is active
#if defined(WIRE_ROLE_PPU)
    display_loop();
#else
    emu_host_loop();
#endif
}

#if !defined(WIRE_ROLE_PPU)
static void emu_host_loop()
{
    uint32_t frame_start_time = millis();
    uint32_t t;

    get_input();
    cpu_instance.memory[INPUT_ADDRESS] = input_state;

    t = micros();
    run_frame_instructions();
    uint32_t us_emu = micros() - t;

    if (!g_pending_rom.isEmpty())
    {
        // initialize_cpu() re-resets the APU, which the audio task reads on the other core. Park the
        // task across the swap, then have it discard the outgoing ROM's queued commands.
        if (audio_task_handle)
            vTaskSuspend(audio_task_handle);
        initialize_cpu();
#if defined(WIRE_ROLE_CPU)
        ppu_sink_reset();   // clear the PPU chip in lockstep with the local re-init
#endif
        load_rom_from_flash(g_pending_rom.c_str());
        g_apu_flush_req = true;
        if (audio_task_handle)
            vTaskResume(audio_task_handle);
        have_prev_frame = false;   // force the new ROM's first frame to push
        g_pending_rom = "";
        return;
    }

#if defined(WIRE_ROLE_CPU)
    // The frame's pixels went to the PPU chip over the wire during run_frame_instructions — nothing to
    // draw locally. FPS is measured wall-clock frames/sec; emulate is CPU work per frame.
    static uint32_t pn = 0, s_pe = 0, fps_t0 = 0;
    if (fps_t0 == 0) fps_t0 = millis();
    s_pe += us_emu; ++pn;
    uint32_t nowms = millis();
    if (nowms - fps_t0 >= 1000)
    {
        Serial.printf("== FPS %lu | emulate %lu us/frame ==\n",
                      (unsigned long)(pn * 1000UL / (nowms - fps_t0)), (unsigned long)(s_pe / pn));
        pn = s_pe = 0; fps_t0 = nowms;
    }
#else
    // Convert the PPU's composed frame to RGB565 and push it — but only if the pixels changed from the
    // last pushed frame (a pushImage is ~8ms; skipping it on a static screen is a real win).
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
    static uint32_t pn = 0, s_ppu = 0, s_pe = 0, fps_t0 = 0;
    if (fps_t0 == 0) fps_t0 = millis();
    s_ppu += us_ppu; s_pe += us_emu; ++pn;
    uint32_t nowms = millis();
    if (nowms - fps_t0 >= 1000)
    {
        Serial.printf("== FPS %lu | emulate %lu, convert+push %lu (us/frame) ==\n",
                      (unsigned long)(pn * 1000UL / (nowms - fps_t0)),
                      (unsigned long)(s_pe / pn), (unsigned long)(s_ppu / pn));
        pn = s_ppu = s_pe = 0; fps_t0 = nowms;
    }
#endif

    int32_t frame_duration = millis() - frame_start_time;
    if (frame_duration < (int32_t)g_frame_target_ms)
        delay(g_frame_target_ms - frame_duration);
}
#endif  // !WIRE_ROLE_PPU

#if defined(WIRE_ROLE_PPU)
// The PPU chip: run ppu.cpp, receive command streams over the wire, compose, push to the TFT. This is
// the display half of emu_host_loop, fed from the wire instead of a local sys_ppu_submit.
static uint8_t s_ppu_msg[WIRE_MAX_MSG];

static void display_setup()
{
    tft.init();
    tft.setRotation(3);
    tft.setSwapBytes(true);
    tft.fillScreen(TFT_BLACK);
    ppu_reset();
    wire_slave_begin();
    Serial.println("display: receiving PPU commands over the wire.");
}

static void display_loop()
{
    uint8_t type;
    int n = wire_slave_receive(&type, s_ppu_msg, sizeof(s_ppu_msg));
    if (n < 0) return;   // spurious/short transaction, ignore
    switch (type)
    {
    case MSG_PPU_CMD:
        if (ppu_receive(s_ppu_msg, n))   // true iff the stream ended in PRESENT
        {
            ppu_convert_rgb565(frame_buffer);
            tft.pushImage(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT, frame_buffer);
        }
        break;
    case MSG_PPU_WRITE:                  // [addr:2][bytes...] -> PPU RAM
    {
        uint16_t addr = (uint16_t)s_ppu_msg[0] | ((uint16_t)s_ppu_msg[1] << 8);
        if (n >= 2) ppu_write(addr, s_ppu_msg + 2, (uint32_t)(n - 2));
        break;
    }
    case MSG_RESET:
        ppu_reset();
        break;
    }
}
#endif  // WIRE_ROLE_PPU

// ── Audio output (I2S -> MAX98357A amp) ──────────────────────────────────────

void audio_i2s_begin()
{
    i2s_config_t cfg = {};
    cfg.mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
    cfg.sample_rate          = AUDIO_RATE;
    cfg.bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT;
    // Stereo, with the same mono sample in both slots: that plays correctly whichever way the amp's
    // SD pin straps its channel select ((L+R)/2, Left-only, or Right-only).
    cfg.channel_format       = I2S_CHANNEL_FMT_RIGHT_LEFT;
    cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
    cfg.intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1;
    cfg.dma_buf_count        = 4;
    cfg.dma_buf_len          = AUDIO_BLOCK;
    cfg.use_apll             = false;
    cfg.tx_desc_auto_clear   = true;   // emit silence rather than stale garbage on an underrun

    i2s_pin_config_t pins = {};
    pins.mck_io_num   = I2S_PIN_NO_CHANGE;
    pins.bck_io_num   = I2S_BCLK_PIN;
    pins.ws_io_num    = I2S_LRC_PIN;
    pins.data_out_num = I2S_DOUT_PIN;
    pins.data_in_num  = I2S_PIN_NO_CHANGE;   // TX only

    esp_err_t e = i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
    if (e != ESP_OK && ENABLE_DEBUG_LOGS)
        Serial.printf("i2s_driver_install failed: %d\n", (int)e);
    e = i2s_set_pin(I2S_NUM_0, &pins);
    if (e != ESP_OK && ENABLE_DEBUG_LOGS)
        Serial.printf("i2s_set_pin failed: %d\n", (int)e);
}

// ── The audio task — the APU's own clock ─────────────────────────────────────
// This is the "two clocks" principle made physical. apu_render() free-runs on the OTHER core from
// the game loop, feeding the I2S DMA; i2s_write blocks until the DMA drains, so the task self-paces
// at exactly APU_RATE and CANNOT be starved by a slow game frame. loop() never produces a sample --
// it only pokes sparse voice-state commands, so no audio ever waits on the emulator.
//
// Core/priority: Arduino's loopTask is pinned to core 1 at priority 1
// (CONFIG_ARDUINO_RUNNING_CORE=1), so audio takes core 0 at a higher priority. It does a few ms of
// work then blocks on DMA, so a high priority starves nothing.
static constexpr int AUDIO_CORE      = 0;
static constexpr int AUDIO_TASK_PRIO = 5;
static constexpr int AUDIO_TASK_STACK = 4096;

// Static, not stack: the task's own stack stays small.
static int16_t audio_mono[AUDIO_BLOCK];
static int16_t audio_stereo[AUDIO_BLOCK * 2];

static void audio_task(void *)
{
    for (;;)
    {
        // Apply everything the game core queued since the last block. apu_receive AND apu_render
        // both run here, on this core -- that is precisely what removes the race.
        size_t n;
        if (g_apu_flush_req)
        {
            // A ROM swap happened: discard the outgoing ROM's queued commands so they can't leak
            // into the new one (initialize_cpu already re-reset the APU while we were parked).
            while (xMessageBufferReceive(audio_cmd_buf, audio_cmd_scratch, sizeof(audio_cmd_scratch), 0) > 0)
                ;
            g_apu_flush_req = false;
        }
        while ((n = xMessageBufferReceive(audio_cmd_buf, audio_cmd_scratch, sizeof(audio_cmd_scratch), 0)) > 0)
            apu_receive(audio_cmd_scratch, (int)n);

        apu_render(audio_mono, AUDIO_BLOCK);
        // The APU is mono; the I2S link runs stereo with the SAME sample in both slots, which is
        // what makes us immune to however the amp's SD pin straps its channel select (see
        // audio_i2s_begin).
        for (int i = 0; i < AUDIO_BLOCK; ++i)
        {
            audio_stereo[i * 2]     = audio_mono[i];
            audio_stereo[i * 2 + 1] = audio_mono[i];
        }
        size_t written = 0;
        i2s_write(I2S_NUM_0, audio_stereo, sizeof(audio_stereo), &written, portMAX_DELAY);

        // A heartbeat: a steady ~22050 Hz proves the task is rendering and the DMA is draining.
        static uint32_t hb_frames = 0, hb_t0 = 0;
        hb_frames += written / 4;   // 4 bytes per stereo frame
        uint32_t now = millis();
        if (hb_t0 == 0)
            hb_t0 = now;
        if (now - hb_t0 >= 5000)
        {
            if (ENABLE_DEBUG_LOGS)
                Serial.printf("APU task alive on core %d: %lu frames in %lu ms (~%lu Hz), dropped=%lu\n",
                              xPortGetCoreID(), hb_frames, now - hb_t0,
                              (hb_frames * 1000) / (now - hb_t0), g_apu_dropped);
            hb_frames = 0;
            hb_t0 = now;
        }
    }
}

void audio_task_begin()
{
    audio_cmd_buf = xMessageBufferCreate(APU_CMD_QUEUE);
    if (!audio_cmd_buf)
    {
        if (ENABLE_DEBUG_LOGS)
            Serial.println("xMessageBufferCreate failed -- no audio.");
        return;
    }
    xTaskCreatePinnedToCore(audio_task, "apu", AUDIO_TASK_STACK, NULL,
                            AUDIO_TASK_PRIO, &audio_task_handle, AUDIO_CORE);
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
