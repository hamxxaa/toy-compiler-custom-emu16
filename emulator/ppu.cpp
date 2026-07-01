// EMU16 PPU implementation — see ppu.h for the unit's contract and invariants.
//
// The PPU has its OWN graphics RAM (the `ppu` struct below); it never aliases the CPU's memory[].
// Everything reaches it as commands (ppu_receive) or bulk uploads (ppu_write_region). This is what
// lets the whole unit later move onto a second chip behind SPI with zero protocol change.
#include "ppu.h"
#include "definitions.h"
#include <cstring>

// ── PPU graphics RAM (~56 KB, host-side; NOT the guest's 64 KB memory[]) ─────────────────────────
struct Sprite { uint8_t pat; int16_t x, y; uint8_t attr; }; // attr b0 flipx, b1 flipy, b2 behind_bg
struct Cell   { uint8_t glyph, color; };                    // text-plane cell (glyph 0 = empty)

static struct {
    uint8_t  pat[128][256];   // 16x16 full-color patterns (tiles + sprites); index 0 = backdrop/transparent
    uint8_t  font[128][8];    // 8x8 1-bit glyphs (byte/row, MSB left)
    uint8_t  tilemap[32 * 32];// bg tile ids; torus (512x512 px)
    Cell     textmap[20 * 16];// text-plane cells
    Sprite   oam[64];
    uint8_t  oam_count;
    uint16_t pal[256];        // RGB565 (entry 0 = backdrop)
    uint16_t scroll_x, scroll_y, ctrl;
    uint8_t  fb_idx[SCREEN_WIDTH * SCREEN_HEIGHT]; // indexed compose target -> RGB565 at push
} ppu;

static uint8_t s_inbuf[2048];  // inbound command buffer (models the 2 KB double-buffer / SPI RX)
static bool    s_engaged = false;

static inline uint16_t rd16(const uint8_t* p) { return (uint16_t)(p[0] | (p[1] << 8)); }

void ppu_reset()
{
    memset(&ppu, 0, sizeof(ppu));
    s_engaged = false;
}

// ── Compose: build fb_idx back-to-front ──────────────────────────────────────────────────────────
// Phase 0 paints only the backdrop (index 0). The three planes land in their phases:
//   Phase 1: compose_bg      — tilemap -> fb_idx (run-opt loop, scroll)
//   Phase 2: compose_sprites — OAM, transparency/flip, behind-bg + front passes
//   Phase 3: compose_text    — 8x8 glyphs from the text plane
static void ppu_compose()
{
    memset(ppu.fb_idx, 0, sizeof(ppu.fb_idx));  // clear to the backdrop (palette index 0)
    // (compose_bg / compose_sprites / compose_text slot in here in phases 1-3)
    s_engaged = true;
}

// ── Command decoder — the full wire protocol (little-endian, terminated by PRESENT) ──────────────
// Ops: 0x01 SET_REG, 0x02 OAM, 0x03 TM_EDIT, 0x04 TEXT, 0x05 PAL, 0x06 PRESENT. All six are decoded
// here so the wire format is fixed from Phase 0; phases 1-3 only add COMPOSE logic, never new decode.
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
            if      (reg == 0) ppu.scroll_x = val;
            else if (reg == 1) ppu.scroll_y = val;
            else if (reg == 2) ppu.ctrl     = val;
            break;
        }
        case 0x02: // OAM [count:1][ count x {pat, x:i16, y:i16, attr} ]
        {
            int n = b[i++];
            if (n > 64) n = 64;
            for (int s = 0; s < n; ++s)
            {
                ppu.oam[s].pat  = b[i];
                ppu.oam[s].x    = (int16_t)rd16(b + i + 1);
                ppu.oam[s].y    = (int16_t)rd16(b + i + 3);
                ppu.oam[s].attr = b[i + 5];
                i += 6;
            }
            ppu.oam_count = (uint8_t)n;
            break;
        }
        case 0x03: // TM_EDIT [x:1][y:1][w:1][h:1][ w*h tiles ]  (torus-wrapped)
        {
            int x = b[i], y = b[i + 1], w = b[i + 2], h = b[i + 3];
            i += 4;
            for (int r = 0; r < h; ++r)
                for (int c = 0; c < w; ++c)
                    ppu.tilemap[((y + r) & 31) * 32 + ((x + c) & 31)] = b[i++];
            break;
        }
        case 0x04: // TEXT [x:1][y:1][w:1][h:1][ w*h x {glyph, color} ]
        {
            int x = b[i], y = b[i + 1], w = b[i + 2], h = b[i + 3];
            i += 4;
            for (int r = 0; r < h; ++r)
                for (int c = 0; c < w; ++c)
                {
                    int idx = (y + r) * 20 + (x + c);
                    ppu.textmap[idx].glyph = b[i];
                    ppu.textmap[idx].color = b[i + 1];
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
                if (f + k < 256) ppu.pal[f + k] = rd16(b + i);
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
    return ppu_execute(s_inbuf, len); // the PPU only ever reads s_inbuf + its own state (I1)
}

uint32_t ppu_write_region(uint8_t target, uint32_t dst_off, const uint8_t* src, uint32_t len)
{
    uint8_t* base;
    uint32_t cap;
    switch (target)
    {
    case PPU_TARGET_PAT:     base = &ppu.pat[0][0];    cap = sizeof(ppu.pat);     break;
    case PPU_TARGET_FONT:    base = &ppu.font[0][0];   cap = sizeof(ppu.font);    break;
    case PPU_TARGET_TILEMAP: base = ppu.tilemap;       cap = sizeof(ppu.tilemap); break;
    case PPU_TARGET_PAL:     base = (uint8_t*)ppu.pal; cap = sizeof(ppu.pal);     break;
    default: return 0;
    }
    if (dst_off >= cap) return 0;
    if (dst_off + len > cap) len = cap - dst_off;
    memcpy(base + dst_off, src, len);
    return len;
}

void ppu_convert_rgb565(uint16_t* out)
{
    for (int i = 0; i < SCREEN_WIDTH * SCREEN_HEIGHT; ++i)
        out[i] = ppu.pal[ppu.fb_idx[i]];
}

bool ppu_engaged() { return s_engaged; }
