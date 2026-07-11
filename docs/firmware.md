# Firmware (ESP32-S3)

A PlatformIO/Arduino project under `firmware/` (open *that* folder in PlatformIO, not the repo
root) that boots the shared emulator core on the real handheld. See
[Architecture](architecture.md#firmware) for how it relates to the other two hosts.

## No second emulator copy

`firmware/src/emu_shim.cpp` and `firmware/src/ppu_shim.cpp` `#include` the real
`emulator/emu.cpp`/`ppu.cpp` directly — there is no forked or hand-ported copy of the CPU/PPU for
Arduino. If you ever need to touch core emulation behavior, you're editing the one shared file, and
firmware picks it up automatically on next build.

## Hardware

Pins (`firmware/src/hw_pins.h`):

| Function | Pins |
|---|---|
| SD card (wired, not yet used — see [Storage](#storage)) | MOSI 35 · MISO 37 · SCK 36 · CS 38 |
| D-pad | LEFT 4 · UP 5 · RIGHT 6 · DOWN 7 |
| Buttons | A 15 · B 16 · X 17 · Y 18 |

All 8 button pins are `INPUT_PULLUP` (active-low: pressed reads `LOW`). Display is driven via
`TFT_eSPI` over SPI (`tft.init()`, rotation 3, byte-swap enabled for RGB565).

## Boot flow (`setup()`)

1. `Serial.begin(115200)`, 3 s delay (lets a serial monitor attach before the first log line).
2. Configure button pins.
3. Init the TFT (or, if `ENABLE_FRAMEBUFFER_TEST` is set, run a solid-green panel self-test and
   return early — bypasses the emulator entirely, useful for isolating "is it the display or the ROM"
   when the screen stays black).
4. `initialize_cpu()` + `init_default_pram()` — reset CPU/PPU state and seed a small default legacy
   palette (8 named colors + a grayscale ramp). The **font is not host-loaded** — it ships inside
   every ROM (`io.lib`'s `font8x8` array, part of DATA), so there's no load-order dependency between
   "font present" and "ROM present."
5. `build_rom_list()` — mount LittleFS, enumerate `*.rom` files at the root.
6. `emu_set_syscall_handler(handle_syscall)`.
7. `load_rom_from_flash("/menu.rom")` — the firmware always boots the menu; games are reached by the
   menu calling `sys_load_rom`.
8. Zero VRAM, force the legacy dirty-check state to an impossible value (`0xFF`) so the very first
   frame always rebuilds and pushes (there's nothing "previous" to diff against yet).

## Main loop (`loop()`)

Per iteration: read buttons into the guest `INPUT` byte → `run_frame_instructions()` (one budgeted
batch of CPU execution) → if a `sys_load_rom`/`sys_reset` syscall set a pending ROM, swap it in
(reset CPU/PPU, reload, re-arm the dirty-check sentinels, `have_prev_frame = false`) → otherwise
display.

**Display path picks one of two branches, mutually exclusive per ROM:**

- **PPU engaged** (`ppu_engaged()` — true once a ROM has submitted at least one `PRESENT`): convert
  the PPU's composed indexed framebuffer to RGB565, then **only push to the TFT if the converted
  pixels differ from the last pushed frame** (`memcmp` against `prev_frame_buffer`). Games call
  `sys_present()`/`ppu_present()` every loop regardless of whether anything changed, so "did the PPU
  produce a new frame" is always true — the real dirty signal is whether the *pixels* changed. A
  `pushImage` SPI transfer is the expensive part (~8 ms), so skipping it on a static frame (a paused
  menu, an idle dialog) is a real win; an animating/scrolling game still pushes every frame, same as
  it always would.
- **Legacy VRAM/PRAM path** (no PPU engagement): separate dirty-checks for the palette (`PRAM`, 512 B)
  and the framebuffer (`VRAM`, 20,480 B) — rebuild the cached RGB565 palette only if `PRAM` changed,
  rebuild+push the framebuffer only if `VRAM` changed. Per-phase timing (emulate / pram-check /
  pram-build / vram-check / vram-build / push) is averaged and printed to `Serial` every 60 frames
  when `ENABLE_DEBUG_LOGS` is on — this is the firmware's own built-in profiler, useful for judging
  whether a game is CPU-bound or push-bound. `examples/perftest.txt` was a dedicated benchmark ROM
  for exactly this path (since retired — see the [README](../README.md)'s directory-layout note on
  retired examples).

Frame pacing: after the emulate+display work, `loop()` sleeps out the remainder of
`g_frame_target_ms` (set by `sys_set_fps`, default 16 ms ≈ 60 Hz) if the frame finished early.

## Syscall handler

`handle_syscall()` in `main.cpp` implements every syscall in [syscalls.md](syscalls.md#reference)
against real flash storage:

- **ROM list/load (1-4)** — enumerated once at boot (`g_rom_list`); `sys_load_rom`/`sys_reset` halt
  the CPU and set `g_pending_rom`, actually swapped in at the top of the next `loop()` (not
  mid-syscall — the CPU has to fully unwind first).
- **Save/asset syscalls (7-11)** — namespaced by `g_current_rom` (the currently loaded ROM's
  basename, set on every load), via [`storage.h`](#storage)'s `write_file`/`read_file`/`open_ro`.
  The asset pack's TOC (`g_toc`, up to 64 entries) is parsed once per ROM load
  (`load_asset_toc()`) from `/<rom>.pak`; the blob bytes themselves stay on flash and are streamed
  on demand (`f.seek(offset)` + a 512-byte-chunked read loop for `sys_ppu_dma`, a direct
  bounded read for `sys_asset_load`).
- **`sys_set_fps` (12)** sets `g_frame_target_ms` directly, affecting the sleep at the end of every
  `loop()` iteration from the next frame on.
- **PPU syscalls (13-15)** — thin passthroughs to `ppu_receive`/`ppu_write` (see
  [Architecture → PPU](architecture.md#ppu)); `sys_ppu_submit` additionally calls
  `emu_request_present()` when the command stream ended in `PRESENT`, which is what lets the CPU
  core track frame-yield state consistently across all three hosts.

## Storage

[`firmware/src/storage.h`](../firmware/src/storage.h) is the **entire** filesystem seam: a
`STORAGE_FS` macro (currently `LittleFS`) plus five tiny wrapper functions (`write_file`,
`read_file`, `exists`, `open_ro`, `ensure_dir`). Nothing else in `main.cpp` talks to LittleFS
directly. Migrating to SD (the wired-but-unused SD reader — see [Hardware](#hardware)) later is
meant to be a one-line change here: point `STORAGE_FS` at the SD filesystem object and ensure it's
`.begin()`'d in `setup()`. Both `.pak` reads and save read/writes go through this seam.

## Toggles (`main.cpp`)

- `ENABLE_DEBUG_LOGS` (default `true`) — serial output: ROM load messages, `build_rom_list` count,
  the per-frame timing breakdown described above.
- `ENABLE_FRAMEBUFFER_TEST` (default `false`) — bypasses the emulator entirely and just fills the
  screen solid green every loop; a hardware-only self-test to isolate display-wiring issues from
  emulator/ROM issues.

## Build & flash

```bash
make flash       # cd firmware && pio run -t upload
make uploadfs    # cd firmware && pio run -t uploadfs   (pushes firmware/data/*.rom to LittleFS)
make monitor     # cd firmware && pio device monitor -b 115200
```

Drop a compiled `.rom` into `firmware/data/`, `make uploadfs`, and it appears in the menu's ROM list
on next boot (or immediately, since `build_rom_list()` re-mounts and re-scans LittleFS on the
firmware's own boot, not just at flash time).
