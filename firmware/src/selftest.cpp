// On-device bring-up self-tests — see selftest.h for the DIAG selector.
#include <Arduino.h>
#include <TFT_eSPI.h>
#include <driver/i2s.h>
#include "selftest.h"
#include "definitions.h"
#include "hw_pins.h"
#include "apu.h"
#include "wire.h"

// Defined in main.cpp.
extern TFT_eSPI tft;
extern uint16_t frame_buffer[];
void audio_i2s_begin();
void audio_task_begin();

static void tft_test_init()
{
    tft.init();
    tft.setRotation(3);
    tft.setSwapBytes(true);
    tft.fillScreen(TFT_BLACK);
}

#if !defined(WIRE_ROLE_PPU)
// Solid green — is the panel wired and driven at all?
static void framebuffer_green()
{
    for (int i = 0; i < SCREEN_WIDTH * SCREEN_HEIGHT; ++i)
        frame_buffer[i] = TFT_GREEN;
    tft.pushImage(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT, frame_buffer);
}

// A 440 Hz sine, one cycle in 32 steps at ~1/3 full scale. The top 5 bits of a 32-bit phase
// accumulator index the table; inc = 440 * 2^32 / 22050.
static const int16_t TONE_SINE[32] = {
         0,   1951,   3827,   5556,   7071,   8315,   9239,   9808,
     10000,   9808,   9239,   8315,   7071,   5556,   3827,   1951,
         0,  -1951,  -3827,  -5556,  -7071,  -8315,  -9239,  -9808,
    -10000,  -9808,  -9239,  -8315,  -7071,  -5556,  -3827,  -1951,
};
static constexpr uint32_t TONE_INC   = 85704562;
static constexpr int      TONE_BLOCK = 256;

static void tone_write_block()
{
    static uint32_t phase = 0;
    int16_t block[TONE_BLOCK * 2];   // interleaved L,R — the same sample in both slots
    for (int i = 0; i < TONE_BLOCK; ++i)
    {
        int16_t s = TONE_SINE[(phase >> 27) & 31];
        phase += TONE_INC;
        block[i * 2] = block[i * 2 + 1] = s;
    }
    size_t written = 0;
    i2s_write(I2S_NUM_0, block, sizeof(block), &written, portMAX_DELAY);   // blocks on the DMA = self-paced
}

// A repeating decaying A4 on voice 0: a looping volume macro (DEF_INST_VOL) then NOTE_ON_INST.
static const uint8_t APU_BLIP[] = {
    0x10, 0, 0, 16,   12, 10, 8, 6, 5, 4, 3, 2, 1, 1, 0, 0, 0, 0, 0, 0,
    0x11, 0, 69, 0,
};
#endif  // !WIRE_ROLE_PPU

#if defined(WIRE_ROLE_PPU)
// Color bars + edge border + corner markers. The leftmost bar must read RED (else the color order is
// wrong); the border must sit flush to all four edges (else the ST7735 offset is wrong).
static void tft_bars()
{
    const int W = tft.width(), H = tft.height();
    const uint16_t bars[7] = { TFT_RED, TFT_GREEN, TFT_BLUE, TFT_YELLOW, TFT_CYAN, TFT_MAGENTA, TFT_WHITE };
    const int bw = W / 7;
    for (int i = 0; i < 7; ++i)
        tft.fillRect(i * bw, 0, (i == 6 ? W - 6 * bw : bw), H, bars[i]);
    tft.drawRect(0, 0, W, H, TFT_WHITE);
    tft.fillRect(0, 0, 6, 6, TFT_WHITE);
    tft.fillRect(W - 6, 0, 6, 6, TFT_WHITE);
    tft.fillRect(0, H - 6, 6, 6, TFT_WHITE);
    tft.fillRect(W - 6, H - 6, 6, 6, TFT_WHITE);
    tft.setTextColor(TFT_BLACK, TFT_WHITE);
    tft.setCursor(W / 2 - 34, H / 2 - 4);
    tft.print(" TFT OK ");
}
#endif

#if defined(WIRE_ROLE_CPU)
// A bouncing yellow sprite on a navy backdrop, composed on the PPU chip — proves the command path
// (a pattern upload plus per-frame PAL/OAM/PRESENT) end to end over the wire.
static void testpattern_setup()
{
    wire_master_begin();
    static uint8_t wr[2 + 256];
    wr[0] = 0x00; wr[1] = 0x01;                 // 256 bytes to PPU pattern slot 1
    for (int i = 0; i < 256; ++i) wr[2 + i] = 1;
    wire_send(MSG_PPU_WRITE, wr, sizeof(wr));
}

static void testpattern_loop()
{
    static int x = 8, y = 8, dx = 3, dy = 2;
    uint8_t cmd[32];
    int p = 0;
    cmd[p++] = 0x05; cmd[p++] = 0; cmd[p++] = 2;         // PAL: navy backdrop + yellow sprite
    cmd[p++] = 0x0F; cmd[p++] = 0x00;
    cmd[p++] = 0xE0; cmd[p++] = 0xFF;
    cmd[p++] = 0x02; cmd[p++] = 1;                       // OAM: 1 sprite, pattern 1, at (x,y)
    cmd[p++] = 1;
    cmd[p++] = x & 0xFF; cmd[p++] = (x >> 8) & 0xFF;
    cmd[p++] = y & 0xFF; cmd[p++] = (y >> 8) & 0xFF;
    cmd[p++] = 0;
    cmd[p++] = 0x06;                                     // PRESENT
    wire_send(MSG_PPU_CMD, cmd, p);
    x += dx; if (x < 0 || x > SCREEN_WIDTH  - 16) { dx = -dx; x += dx; }
    y += dy; if (y < 0 || y > SCREEN_HEIGHT - 16) { dy = -dy; y += dy; }
    delay(16);
}
#endif

bool selftest_setup()
{
    if (DIAG == Diag::None) return false;
#if !defined(WIRE_ROLE_PPU)
    if (DIAG == Diag::Framebuffer) { tft_test_init(); framebuffer_green(); return true; }
    if (DIAG == Diag::AudioTone)   { audio_i2s_begin(); return true; }
    if (DIAG == Diag::ApuBlip)     { audio_i2s_begin(); apu_receive(APU_BLIP, sizeof(APU_BLIP)); audio_task_begin(); return true; }
#endif
#if defined(WIRE_ROLE_CPU)
    if (DIAG == Diag::WireLoopback) { wire_test_setup(); return true; }
    if (DIAG == Diag::TestPattern)  { testpattern_setup(); return true; }
#endif
#if defined(WIRE_ROLE_PPU)
    if (DIAG == Diag::TftBars) { tft_test_init(); tft_bars(); return true; }
#endif
    return false;
}

bool selftest_loop()
{
    if (DIAG == Diag::None) return false;
#if !defined(WIRE_ROLE_PPU)
    if (DIAG == Diag::Framebuffer) { framebuffer_green(); delay(16); return true; }
    if (DIAG == Diag::AudioTone)   { tone_write_block(); return true; }
    if (DIAG == Diag::ApuBlip)     { delay(100); return true; }   // the audio task is playing it
#endif
#if defined(WIRE_ROLE_CPU)
    if (DIAG == Diag::WireLoopback) { wire_test_loop(); return true; }
    if (DIAG == Diag::TestPattern)  { testpattern_loop(); return true; }
#endif
#if defined(WIRE_ROLE_PPU)
    if (DIAG == Diag::TftBars) { delay(500); return true; }       // static screen, nothing to animate
#endif
    return false;
}
