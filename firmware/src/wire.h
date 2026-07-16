// wire.h — the inter-chip SPI link for the two-chip CPU/PPU split.
//
// The CPU chip is the SPI MASTER, the PPU chip is the SPI SLAVE; the READY line (PPU->CPU) is
// flow-control / frame-done. See docs/firmware.md.
//
// ROLE SELECTION is a compile-time build flag (each chip gets a different binary):
//   -DWIRE_ROLE_CPU   (the clone board)     -> master
//   -DWIRE_ROLE_PPU   (the original board)  -> slave
//   neither defined                          -> single-chip firmware, wire unused
#pragma once
#include <cstdint>
#include <cstddef>

// ── Message protocol ─────────────────────────────────────────────────────────
enum WireMsg : uint8_t {
    MSG_PING       = 0x00,   // link loopback: payload -> slave returns its FNV checksum
    MSG_PPU_CMD    = 0x01,   // a PPU command stream; may end in PRESENT
    MSG_PPU_WRITE  = 0x02,   // write bytes to PPU RAM at an addr (merges sys_ppu_dma + upload)
    MSG_RESET      = 0x03,   // clear PPU state
};

// ── Inter-chip pins — DIFFERENT per role (see the two boards' real pinouts) ──────────────────────
// N16R8 on both boards: octal PSRAM reserves GPIO 35-37, so the wire deliberately avoids them.
#if defined(WIRE_ROLE_CPU)
  // CPU chip = the clone. Reuses the pins freed by removing the TFT (was RST/DC/CS/MOSI/SCLK 8-12).
  #define WIRE_SCLK   12
  #define WIRE_MOSI   11
  #define WIRE_MISO   10
  #define WIRE_CS      9
  #define WIRE_READY   8   // input: PPU raises it when idle/armed, drops it while busy
#elif defined(WIRE_ROLE_PPU)
  // PPU chip = the original ESP32-S3-DevKitC-1. TFT will live on 8-12 (mirrors the clone); the
  // inter-chip slave takes 4-7, READY-out on 15. All clear of strapping/USB/UART0/flash/PSRAM/LED.
  #define WIRE_SCLK    4
  #define WIRE_MOSI    5
  #define WIRE_MISO    6
  #define WIRE_CS      7
  #define WIRE_READY  15   // output: high = idle/armed, low = mid-transaction
#endif

// True on either two-chip role; false for the single-chip build.
#if defined(WIRE_ROLE_CPU) || defined(WIRE_ROLE_PPU)
  #define WIRE_ENABLED 1
#else
  #define WIRE_ENABLED 0
#endif

// Biggest message the link buffers in one transaction (header + payload, padded to a multiple of 4).
// Covers the 2 KB PPU command buffer + small asset writes; large asset DMAs chunk to this size.
#define WIRE_MAX_MSG    4096
#define WIRE_TIMEOUT_MS 1000   // how long the master waits on READY before declaring the link down

// FNV-1a 32-bit — the loopback checksum (same on both chips).
uint32_t wire_fnv1a(const uint8_t *data, size_t len);

// ── Transport API — the CPU<->PPU link ───────────────────────────────────────
// Message on the wire: [type:1][len_lo][len_hi][flags:1] then `len` payload bytes, padded to a
// multiple of 4 (ESP32 SPI-DMA needs word-aligned, 4-byte-multiple transfers). READY paces it: the
// master waits for the slave to raise READY before each send.
#if defined(WIRE_ROLE_CPU)
void wire_master_begin();
void wire_send(uint8_t type, const uint8_t *payload, uint16_t len);   // waits READY, sends one message
#elif defined(WIRE_ROLE_PPU)
void wire_slave_begin();
int  wire_slave_receive(uint8_t *type_out, uint8_t *buf, int maxlen); // blocks for one message; -1 = ignore
#endif

// Loopback self-test entry points (the MSG_PING check; see selftest.h).
void wire_test_setup();
void wire_test_loop();
