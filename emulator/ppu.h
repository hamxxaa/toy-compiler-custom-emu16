// EMU16 PPU — a NES-2C02-style tile + sprite compositor that owns all graphics RAM and the
// display, driven by a serializable command stream the CPU fills each frame (see the plan
// tiling-scrolling-ricoh.md). It is a SEPARATE unit from the CPU: it never touches the CPU's
// memory[] (invariant I1) — the host/bus copies command bytes IN, the PPU only reads its own
// state. On one chip this is a function call; on two chips the same bytes cross SPI.
//
// Phase 0 = the seam: the module, the full wire protocol (decoder), and a backdrop-only compose.
// compose_bg / compose_sprites / compose_text land in phases 1-3; the wire format is fixed now.
#pragma once
#include <cstdint>

// Bulk-upload targets for sys_ppu_dma / sys_ppu_upload (a byte offset addresses within each).
enum {
    PPU_TARGET_PAT     = 0,   // pat[128][256]  — 16x16 full-color patterns (tiles + sprites)
    PPU_TARGET_FONT    = 1,   // font[128][8]   — 8x8 1-bit glyphs
    PPU_TARGET_TILEMAP = 2,   // tilemap[32*32] — bg tile ids (torus)
    PPU_TARGET_PAL     = 3,   // pal[256]       — RGB565, little-endian
};

// Reset all PPU graphics RAM + state. Called from initialize_cpu() so every host (pc_emu / WASM /
// firmware) resets the PPU together with the CPU.
void ppu_reset();

// Inbound transport (models the SPI RX on a 2-chip split): copy `len` command bytes from `src`
// (a pointer into CPU memory — the host/bus performs this copy) into the PPU's own 2 KB command
// buffer, then execute the stream. The PPU itself only ever reads that buffer + its own state (I1).
// Returns true if the stream ended in PRESENT (a frame was composed).
bool ppu_receive(const uint8_t* src, int len);

// Bulk region write (sys_ppu_upload / the local half of sys_ppu_dma): copy `len` bytes from `src`
// (CPU memory) into PPU region `target` at byte offset `dst_off`. Bounds-checked to the region;
// returns the number of bytes actually written.
uint32_t ppu_write_region(uint8_t target, uint32_t dst_off, const uint8_t* src, uint32_t len);

// Display driver (the PPU owns the display, I4): convert the composed indexed framebuffer to
// RGB565 through the PPU palette. `out` must hold SCREEN_WIDTH*SCREEN_HEIGHT uint16_t.
void ppu_convert_rgb565(uint16_t* out);

// True once the PPU has composed at least one frame since reset. Hosts use this to display the
// PPU output instead of the legacy VRAM framebuffer while both paths coexist (phases 0-3).
bool ppu_engaged();
