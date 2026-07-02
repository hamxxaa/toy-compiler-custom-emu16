// EMU16 PPU implementation — see ppu.h for the unit's contract and invariants.
//
// The PPU has its OWN graphics RAM: one flat byte array `ppu_instance.memory[]`, addressed by the
// region-offset constants below. This mirrors the CPU exactly (`cpu_instance.memory[65536]` in emu.h
// + the memory map in definitions.h) — same flat-array + named-offsets style, little-endian words —
// so once you understand one, the other reads the same. No per-field structs (and no struct padding).
// It never aliases the CPU's memory[]; everything reaches it as commands (ppu_receive) or bulk
// uploads (ppu_write). That is what lets the whole unit later move onto a second chip behind SPI.
#include "ppu.h"
#include "definitions.h"
#include <cstring>

// ── PPU graphics RAM map (one flat array; offsets in the same spirit as definitions.h) ───────────
//  0x0000  PAT      128*256 = 32768   16x16 full-color patterns (tiles + sprites); idx 0 = backdrop/transparent
//  0x8000  FONT     128*8   = 1024    8x8 1-bit glyphs (byte/row, MSB left)
//  0x8400  TILEMAP  32*32   = 1024    bg tile ids; torus (512x512 px)
//  0x8800  TEXTMAP  20*16*2 = 640     text-plane cells {glyph:u8, color:u8}
//  0x8A80  OAM      64*6    = 384     sprites {pat:u8, x:i16, y:i16, attr:u8}
//  0x8C00  PAL      256*2   = 512     RGB565, little-endian (entry 0 = backdrop)
//  0x8E00  REGS     16                scroll_x:u16, scroll_y:u16, ctrl:u16, oam_count:u8
//  0x8E10  FB       160*128 = 20480   indexed compose target -> RGB565 at push
//  0xDE10  end (PPU_MEM_SIZE ≈ 55.5 KB)
#define PPU_PAT            0x0000
#define PPU_PAT_SIZE       (128 * 256)
#define PPU_PAT_STRIDE     256                 // bytes per 16x16 pattern
#define PPU_FONT           (PPU_PAT + PPU_PAT_SIZE)          // 0x8000
#define PPU_FONT_SIZE      (128 * 8)
#define PPU_TILEMAP        (PPU_FONT + PPU_FONT_SIZE)        // 0x8400
#define PPU_TILEMAP_SIZE   (32 * 32)
#define PPU_TEXTMAP        (PPU_TILEMAP + PPU_TILEMAP_SIZE)  // 0x8800
#define PPU_TEXTMAP_SIZE   (20 * 16 * 2)
#define PPU_OAM            (PPU_TEXTMAP + PPU_TEXTMAP_SIZE)  // 0x8A80
#define PPU_OAM_SIZE       (64 * 6)
#define PPU_PAL            (PPU_OAM + PPU_OAM_SIZE)          // 0x8C00
#define PPU_PAL_SIZE       (256 * 2)
#define PPU_REGS           (PPU_PAL + PPU_PAL_SIZE)          // 0x8E00
#define PPU_REGS_SIZE      16
#define PPU_FB             (PPU_REGS + PPU_REGS_SIZE)        // 0x8E10
#define PPU_FB_SIZE        (SCREEN_WIDTH * SCREEN_HEIGHT)
#define PPU_MEM_SIZE       (PPU_FB + PPU_FB_SIZE)            // 0xDE10

// REGS sub-fields (little-endian, like the CPU's memory-mapped words).
#define PPU_REG_SCROLL_X   (PPU_REGS + 0)   // u16, window-relative 0..511
#define PPU_REG_SCROLL_Y   (PPU_REGS + 2)   // u16, window-relative 0..511
#define PPU_REG_CTRL       (PPU_REGS + 4)   // u16, layer enables (reserved)
#define PPU_REG_OAM_COUNT  (PPU_REGS + 6)   // u8, active sprites

// Its own flat RAM — the PPU analogue of `struct cpu`'s memory[] (see emu.h).
struct ppu { uint8_t memory[PPU_MEM_SIZE]; };
static ppu  ppu_instance;
static bool ppu_composed = false;   // display latch: has the PPU produced a frame since reset?

static uint8_t  s_inbuf[2048];      // inbound command buffer (models the 2 KB double-buffer / SPI RX)

// Little-endian word access into PPU RAM (mirrors emu.cpp's read_word_le / write_word_le).
static inline uint16_t ppu_read_word(uint32_t a)
{
    return (uint16_t)(ppu_instance.memory[a] | (ppu_instance.memory[a + 1] << 8));
}
static inline void ppu_write_word(uint32_t a, uint16_t v)
{
    ppu_instance.memory[a]     = (uint8_t)(v & 0xFF);
    ppu_instance.memory[a + 1] = (uint8_t)((v >> 8) & 0xFF);
}
// Little-endian word read from the incoming command stream `b`.
static inline uint16_t rd16(const uint8_t* p) { return (uint16_t)(p[0] | (p[1] << 8)); }

void ppu_reset()
{
    memset(ppu_instance.memory, 0, sizeof(ppu_instance.memory));
    ppu_composed = false;
}

// ── Compose: build the indexed framebuffer back-to-front (bg -> sprites -> text) ─────────────────
static void compose_bg()
{
    // The run-opt loop from Exp C: per screen row, copy whole tile-row spans (memcpy) rather than
    // per-pixel. The window is a torus: world coords wrap at 512 (32 tiles * 16 px). A tile pixel of
    // index 0 stays 0 in fb_idx -> shows the backdrop after palette lookup.
    uint16_t scroll_x = ppu_read_word(PPU_REG_SCROLL_X);
    uint16_t scroll_y = ppu_read_word(PPU_REG_SCROLL_Y);
    for (int sy = 0; sy < SCREEN_HEIGHT; ++sy)
    {
        int wy   = (sy + scroll_y) & 511;   // world y (wrapped)
        int trow = (wy >> 4) * 32;          // tile-row base within the tilemap
        int py   = (wy & 15) << 4;          // pixel row inside a 16px tile (row * 16)
        uint8_t* out = &ppu_instance.memory[PPU_FB + sy * SCREEN_WIDTH];
        int sx = 0;
        int wx = scroll_x & 511;            // world x (wrapped)
        while (sx < SCREEN_WIDTH)
        {
            int tx  = wx & 15;              // pixel col inside the current tile
            int run = 16 - tx;              // pixels left in this tile row
            if (run > SCREEN_WIDTH - sx) run = SCREEN_WIDTH - sx;
            uint8_t tile = ppu_instance.memory[PPU_TILEMAP + trow + (wx >> 4)];
            const uint8_t* src = &ppu_instance.memory[PPU_PAT + tile * PPU_PAT_STRIDE + py + tx];
            memcpy(out + sx, src, run);
            sx += run;
            wx = (wx + run) & 511;
        }
    }
}

// Composite the 16x16 sprites in OAM. Called twice: `behind`=1 draws walk-behind sprites (only over
// the backdrop — hidden by any real bg tile pixel), then `behind`=0 draws front sprites over
// everything. Within a pass, later OAM entries draw on top (the CPU emits back-to-front for Y-sort).
// attr: b0 flipx, b1 flipy, b2 behind_bg. Sprite (x,y) is screen-space (the CPU already did world->screen).
static void compose_sprites(int behind)
{
    int count = ppu_instance.memory[PPU_REG_OAM_COUNT];
    for (int s = 0; s < count; ++s)
    {
        uint32_t o    = PPU_OAM + s * 6;
        uint8_t  pat  = ppu_instance.memory[o];
        int16_t  x    = (int16_t)(ppu_instance.memory[o + 1] | (ppu_instance.memory[o + 2] << 8));
        int16_t  y    = (int16_t)(ppu_instance.memory[o + 3] | (ppu_instance.memory[o + 4] << 8));
        uint8_t  attr = ppu_instance.memory[o + 5];
        if (((attr >> 2) & 1) != behind) continue;                 // wrong pass for this sprite
        const uint8_t* psrc = &ppu_instance.memory[PPU_PAT + pat * PPU_PAT_STRIDE];
        for (int row = 0; row < 16; ++row)
        {
            int sy = y + row;
            if (sy < 0 || sy >= SCREEN_HEIGHT) continue;           // clip top/bottom
            int gr = (attr & 2) ? 15 - row : row;                  // flipy
            for (int col = 0; col < 16; ++col)
            {
                int sx = x + col;
                if (sx < 0 || sx >= SCREEN_WIDTH) continue;        // clip left/right
                int gc = (attr & 1) ? 15 - col : col;              // flipx
                uint8_t px = psrc[gr * 16 + gc];
                if (!px) continue;                                 // index 0 = transparent
                uint32_t fi = PPU_FB + sy * SCREEN_WIDTH + sx;
                if (behind && ppu_instance.memory[fi]) continue;   // walk-behind: hidden by a bg tile
                ppu_instance.memory[fi] = px;
            }
        }
    }
}

// The text plane: 20x16 cells (exactly 160x128 px), each {glyph:u8, color:u8}. A non-zero glyph blits
// its 8x8 1-bit font bitmap (from PPU_FONT) in the cell's color, over everything else — HUD + dialog.
// glyph 0 = empty (skipped). Cells tile the screen exactly, so no clipping is needed.
static void compose_text()
{
    for (int cy = 0; cy < 16; ++cy)
        for (int cx = 0; cx < 20; ++cx)
        {
            uint32_t cell  = PPU_TEXTMAP + (cy * 20 + cx) * 2;
            uint8_t  glyph = ppu_instance.memory[cell];
            if (!glyph) continue;
            uint8_t  color = ppu_instance.memory[cell + 1];
            const uint8_t* g = &ppu_instance.memory[PPU_FONT + glyph * 8];
            for (int r = 0; r < 8; ++r)
            {
                uint8_t bits = g[r];
                if (!bits) continue;
                uint8_t* out = &ppu_instance.memory[PPU_FB + (cy * 8 + r) * SCREEN_WIDTH + cx * 8];
                for (int b = 0; b < 8; ++b)
                    if (bits & (0x80 >> b)) out[b] = color;
            }
        }
}

static void ppu_compose()
{
    compose_bg();
    compose_sprites(1);   // behind-bg sprites (walk-behind)
    compose_sprites(0);   // front sprites
    compose_text();       // HUD / dialog, over everything
    ppu_composed = true;
}

// ── Command decoder — the full wire protocol (little-endian, terminated by PRESENT) ──────────────
// Ops: 0x01 SET_REG, 0x02 OAM, 0x03 TM_EDIT, 0x04 TEXT, 0x05 PAL, 0x06 PRESENT. All six are decoded
// here so the wire format is fixed from Phase 0; later phases only add COMPOSE logic, never decode.
static bool ppu_execute(const uint8_t* b, int len)
{
    for (int i = 0; i < len;)
    {
        uint8_t op = b[i++];
        switch (op)
        {
        case 0x01: // SET_REG [reg:1][val:2]  (0 scroll_x, 1 scroll_y, 2 ctrl)
        {
            uint8_t  reg = b[i];
            uint16_t val = rd16(b + i + 1);
            i += 3;
            if      (reg == 0) ppu_write_word(PPU_REG_SCROLL_X, val);
            else if (reg == 1) ppu_write_word(PPU_REG_SCROLL_Y, val);
            else if (reg == 2) ppu_write_word(PPU_REG_CTRL, val);
            break;
        }
        case 0x02: // OAM [count:1][ count x {pat, x:i16, y:i16, attr} ]  (6 bytes/sprite, same layout as RAM)
        {
            int n = b[i++];
            if (n > 64) n = 64;
            memcpy(&ppu_instance.memory[PPU_OAM], b + i, (size_t)n * 6);
            i += n * 6;
            ppu_instance.memory[PPU_REG_OAM_COUNT] = (uint8_t)n;
            break;
        }
        case 0x03: // TM_EDIT [x:1][y:1][w:1][h:1][ w*h tiles ]  (torus-wrapped)
        {
            int x = b[i], y = b[i + 1], w = b[i + 2], h = b[i + 3];
            i += 4;
            for (int r = 0; r < h; ++r)
                for (int c = 0; c < w; ++c)
                    ppu_instance.memory[PPU_TILEMAP + ((y + r) & 31) * 32 + ((x + c) & 31)] = b[i++];
            break;
        }
        case 0x04: // TEXT [x:1][y:1][w:1][h:1][ w*h x {glyph, color} ]
        {
            int x = b[i], y = b[i + 1], w = b[i + 2], h = b[i + 3];
            i += 4;
            for (int r = 0; r < h; ++r)
                for (int c = 0; c < w; ++c)
                {
                    uint32_t o = PPU_TEXTMAP + ((y + r) * 20 + (x + c)) * 2;
                    ppu_instance.memory[o]     = b[i];
                    ppu_instance.memory[o + 1] = b[i + 1];
                    i += 2;
                }
            break;
        }
        case 0x05: // PAL [first:1][count:1][ count x rgb565:2 ]
        {
            int f = b[i], n = b[i + 1];
            i += 2;
            for (int k = 0; k < n; ++k)
            {
                if (f + k < 256) ppu_write_word(PPU_PAL + (f + k) * 2, rd16(b + i));
                i += 2;
            }
            break;
        }
        case 0x06: // PRESENT
            ppu_compose();
            return true;
        default:
            // Unknown op: its length is unknown, so we cannot safely resync — stop. With our own
            // encoder this never happens; a well-formed stream returns via PRESENT above.
            return false;
        }
    }
    return false;
}

bool ppu_receive(const uint8_t* src, int len)
{
    if (len < 0) len = 0;
    if (len > (int)sizeof(s_inbuf)) len = (int)sizeof(s_inbuf);
    memcpy(s_inbuf, src, len);        // the bus copies CPU bytes in (models SPI RX)
    return ppu_execute(s_inbuf, len); // the PPU only ever reads s_inbuf + its own RAM (I1)
}

uint32_t ppu_write(uint32_t addr, const uint8_t* src, uint32_t len)
{
    if (addr >= PPU_MEM_SIZE) return 0;
    if (addr + len > PPU_MEM_SIZE) len = PPU_MEM_SIZE - addr;
    memcpy(&ppu_instance.memory[addr], src, len);
    return len;
}

void ppu_convert_rgb565(uint16_t* out)
{
    for (int i = 0; i < SCREEN_WIDTH * SCREEN_HEIGHT; ++i)
    {
        uint8_t idx = ppu_instance.memory[PPU_FB + i];
        out[i] = ppu_read_word(PPU_PAL + idx * 2);
    }
}

bool ppu_engaged() { return ppu_composed; }
