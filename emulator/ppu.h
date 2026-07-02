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

// The PPU's graphics RAM is one flat, byte-addressed array (see the memory map + region offsets in
// ppu.cpp), exactly like the CPU's memory[]. Bulk uploads target a flat PPU address; the CPU-side
// region base constants (PPU_PAT / PPU_FONT / PPU_TILEMAP / PPU_PAL in lib/ppu.lib) name those
// addresses and MUST match ppu.cpp.

// Reset all PPU graphics RAM + state. Called from initialize_cpu() so every host (pc_emu / WASM /
// firmware) resets the PPU together with the CPU.
void ppu_reset();

// Inbound transport (models the SPI RX on a 2-chip split): copy `len` command bytes from `src`
// (a pointer into CPU memory — the host/bus performs this copy) into the PPU's own 2 KB command
// buffer, then execute the stream. The PPU itself only ever reads that buffer + its own state (I1).
// Returns true if the stream ended in PRESENT (a frame was composed).
bool ppu_receive(const uint8_t* src, int len);

// Bulk write into PPU RAM (sys_ppu_upload from CPU memory / sys_ppu_dma from the pak): copy `len`
// bytes from `src` to flat PPU address `addr`. Bounds-checked; returns the number of bytes written.
uint32_t ppu_write(uint32_t addr, const uint8_t* src, uint32_t len);

// Display driver (the PPU owns the display, I4): convert the composed indexed framebuffer to
// RGB565 through the PPU palette. `out` must hold SCREEN_WIDTH*SCREEN_HEIGHT uint16_t.
void ppu_convert_rgb565(uint16_t* out);

// True once the PPU has composed at least one frame since reset. Hosts use this to display the
// PPU output instead of the legacy VRAM framebuffer while both paths coexist (phases 0-3).
bool ppu_engaged();
