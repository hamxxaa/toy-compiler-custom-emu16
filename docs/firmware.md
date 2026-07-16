# Firmware (ESP32-S3)

A PlatformIO/Arduino project under `firmware/` (open *that* folder in PlatformIO, not the repo
root) that boots the shared emulator core on the real handheld. See
[Architecture](architecture.md#firmware) for how it relates to the other two hosts.

## No second emulator copy

`firmware/src/emu_shim.cpp`, `firmware/src/ppu_shim.cpp`, and `firmware/src/apu_shim.cpp` `#include`
the real `emulator/emu.cpp`/`ppu.cpp`/`apu.cpp` directly — there is no forked or hand-ported copy of
the CPU/PPU/APU for Arduino. If you ever need to touch core emulation behavior, you're editing the
one shared file, and firmware picks it up automatically on next build.

**Audio is live.** A ROM's `sys_apu_submit`/`music_frame()` output real sound: a MAX98357A I2S amp +
speaker, driven by a dedicated audio task on core 0 while the game runs on core 1 (see
[Audio](#audio)). **Two-chip builds** (below) additionally run the display on a *second* ESP32-S3
over an SPI link.

## Hardware

Pins (`firmware/src/hw_pins.h`; the TFT's own pins are TFT_eSPI build flags in
[`firmware/platformio.ini`](../firmware/platformio.ini), not `hw_pins.h`):

| Function | Pins |
|---|---|
| D-pad | LEFT 4 · UP 5 · RIGHT 6 · DOWN 7 |
| Buttons | A 15 · B 16 · X 17 · Y 18 |
| I2S audio → MAX98357A amp | BCLK 13 · LRC 14 · DIN 21 |
| TFT (via `platformio.ini`) | SCLK 12 · MOSI 11 · CS 10 · DC 9 · RST 8 |
| SD card (wired, not yet used — see [Storage](#storage)) | MOSI 35 · MISO 37 · SCK 36 · CS 38 |
| Inter-chip SPI (two-chip only) | SCLK 12 · MOSI 11 · MISO 10 · CS 9 · READY 8 |

All 8 button pins are `INPUT_PULLUP` (active-low: pressed reads `LOW`). Display is driven via
`TFT_eSPI` over SPI (`tft.init()`, rotation 3, byte-swap enabled for RGB565). ⚠️ Both boards are
**ESP32-S3-N16R8** = octal PSRAM, which reserves **GPIO 35-37** — the SD pins above therefore conflict
with PSRAM and must move before SD is wired for real (it's only wired, not yet used). On a two-chip
`cpu` build the TFT is gone and its freed pins (8-12) become the inter-chip SPI master.

## Boot flow (`setup()`)

This is the `single` (one-board) path; `cpu`/`ppu` builds branch to their own setup (the wire link,
and for `ppu` the receive loop) — see [Two chips](#one-or-two-chips-the-build-configs).

1. `Serial.begin(115200)`, 3 s delay (lets a serial monitor attach before the first log line).
2. Configure button pins.
3. Init audio I2S (before the display, so I2S claims its DMA channel cleanly).
4. Init the TFT (or, if `ENABLE_FRAMEBUFFER_TEST` is set, run a solid-green panel self-test and
   return early — bypasses the emulator entirely, useful for isolating "is it the display or the ROM"
   when the screen stays black).
5. `initialize_cpu()` — resets CPU/PPU/APU state. The **font is not host-loaded** — it ships inside every
   ROM (`io.lib`'s `font8x8` array, part of DATA), so there's no load-order dependency between "font
   present" and "ROM present." The host seeds no default palette either, so a ROM that boots standalone
   must upload its own, same as `examples/menu.txt` does.
6. `build_rom_list()` — mount LittleFS, enumerate `*.rom` files at the root.
7. `emu_set_syscall_handler(handle_syscall)`.
8. `load_rom_from_flash("/menu.rom")` — the firmware always boots the menu; games are reached by the
   menu calling `sys_load_rom`.
9. Start the **audio task** last, so all APU state written on this core (the reset above) is in place
   before the other core begins reading it — no boot-time race.

## Main loop (`loop()`)

Per iteration: read buttons into the guest `INPUT` byte → `run_frame_instructions()` (one budgeted
batch of CPU execution) → if a `sys_load_rom`/`sys_reset` syscall set a pending ROM, swap it in
(reset CPU, reload, `have_prev_frame = false` so the new ROM's first frame always pushes) → otherwise
display.

**The display is the PPU path** — every ROM drives the PPU: convert the PPU's composed indexed
framebuffer to RGB565, then **only push to the TFT if the converted pixels differ from the last pushed
frame** (`memcmp` against `prev_frame_buffer`). Games call `sys_present()`/`ppu_present()` every loop regardless of whether
anything changed, so "did the PPU produce a new frame" is always true — the real dirty signal is
whether the *pixels* changed. A `pushImage` SPI transfer is the expensive part (~8 ms), so skipping
it on a static frame (a paused menu, an idle dialog) is a real win; an animating/scrolling game still
pushes every frame, same as it always would. Rolling average timings (emulate / convert+compare+push)
print to `Serial` every 60 frames when `ENABLE_DEBUG_LOGS` is on.

Frame pacing: after the emulate+display work, `loop()` sleeps out the remainder of
`g_frame_target_ms` (set by `sys_set_fps`, default 16 ms ≈ 60 Hz) if the frame finished early.

(On a `cpu` build there is no local display step — the frame's command stream was already sent over
the wire during `run_frame_instructions`, and the *other* chip composed and pushed it in parallel. See
[Two chips](#one-or-two-chips-the-build-configs).)

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
- **PPU syscalls (13-15)** go through a small `ppu_sink` seam: on a `single` build they're thin
  passthroughs to `ppu_receive`/`ppu_write` (see [Architecture → PPU](architecture.md#ppu)); on a
  `cpu` build they become SPI sends to the PPU chip (see [Two chips](#one-or-two-chips-the-build-configs)).
  Either way `sys_ppu_submit` calls `emu_request_present()` on a `PRESENT`-terminated stream, which is
  what lets the CPU core track frame-yield state consistently across all hosts and both configs.
- **APU syscalls (16-17)** — `sys_apu_submit` hands the command bytes to the audio task (never touches
  the synth on this core); `sys_apu_ticks` returns the APU's free-running 60 Hz counter. See
  [Audio](#audio).

## Audio

A ROM's APU output reaches a MAX98357A I2S amp + speaker, with the synth deliberately **decoupled from
the game loop** (the same "two clocks" idea the [APU](architecture.md#apu) is built on):

- A dedicated **audio task on core 0** loops `apu_render()` → `i2s_write()` forever. `i2s_write` blocks
  until the DMA drains, so the task **self-clocks at the APU's sample rate** — no timer. The game loop
  runs on core 1, unaffected.
- `sys_apu_submit` (on core 1) does **not** call the synth. It hands the raw command bytes across to
  the audio task through a FreeRTOS **MessageBuffer** (atomic sends — a batch that doesn't fit is
  dropped whole, never truncated). The task drains it and calls `apu_receive` itself, so the APU's
  voice state is touched by exactly one core: the cross-core race can't exist, no lock needed.
- This is the legacy `driver/i2s.h` API (the pinned Arduino-ESP32 2.0.17 / ESP-IDF 4.4 toolchain ships
  only that; the newer `i2s_std.h` doesn't exist there — see the platform pin in `platformio.ini`).
- Power note: feed the amp its own supply if you can. Sharing the ESP32's 3.3 V rail works but audio
  current spikes can sag it (a real source of intermittent distortion, separate from any code).

## One or two chips (the build configs)

The firmware builds three ways from one tree, selected by a `platformio.ini` env (a `-D WIRE_ROLE_*`
define). Same board (`esp32-s3-devkitc-1`) for all three:

| Env | Runs | Role |
|---|---|---|
| `single` (default) | CPU + PPU + APU, all local | today's one-board handheld |
| `cpu` | `emu.cpp` + `apu.cpp`, inputs, storage, SPI **master** | the game+audio chip of a pair |
| `ppu` | `ppu.cpp` + the TFT, SPI **slave** | the display chip of a pair |

```bash
pio run -e cpu -t upload     # (make flash-cpu) — flash the game chip
pio run -e ppu -t upload     # (make flash-ppu) — flash the display chip
```

A `ppu_sink` seam inside the handler picks where the PPU syscalls go — the local PPU on a `single`
build, the SPI link on a `cpu` build. The shared core and every game are byte-for-byte identical
across all three; only that seam differs. (pc_emu and the browser run everything in one process, so
the split is a firmware-only concept.)

**The wire** (5 signals + common ground): SPI **SCLK/MOSI/MISO/CS** (CPU chip = master) plus a
**READY** line the PPU chip drives — high when it's armed to receive, low while it composes+pushes. The
master waits on READY before each send, which is both flow control and frame pacing. Framed messages
are `[type][len][flags]` + payload padded to a 4-byte multiple (ESP32 SPI-DMA wants word-aligned
transfers); `sys_ppu_dma` and `sys_ppu_upload` merge into one "write these bytes to PPU RAM" message,
since on the CPU chip both just read a source and ship it.

**The two chips run in parallel.** `wire_send` returns once the bytes are transferred — it does *not*
wait for the PPU's compose+push. So the `cpu` board runs the next frame's logic while the `ppu` board
composes and pushes the current one. Frame time is `max(game logic, compose+push)`, not their sum. The
only step that isn't overlapped is the SPI transfer itself (`spi_device_transmit` blocks the sender);
the link runs at 40 MHz to keep it small. A live `== FPS n | emulate x us/frame ==` line prints to the
`cpu` board's serial. Frame pacing (`g_frame_target_ms`, default 16 ms ≈ 60 fps, set by `sys_set_fps`)
stays on the `cpu` board; the `ppu` board has no say in timing.

## Storage

[`firmware/src/storage.h`](../firmware/src/storage.h) is the **entire** filesystem seam: a
`STORAGE_FS` macro (currently `LittleFS`) plus five tiny wrapper functions (`write_file`,
`read_file`, `exists`, `open_ro`, `ensure_dir`). Nothing else in `main.cpp` talks to LittleFS
directly. Migrating to SD (the wired-but-unused SD reader — see [Hardware](#hardware)) later is
meant to be a one-line change here: point `STORAGE_FS` at the SD filesystem object and ensure it's
`.begin()`'d in `setup()`. Both `.pak` reads and save read/writes go through this seam.

## Toggles (`main.cpp`)

Compile-time `constexpr bool`s at the top of `main.cpp`, all defaulting off except logging. Each
bypasses the emulator to exercise one piece of hardware in isolation — the way to tell a wiring fault
from a software one on the display, the amp, or the wire:

- `ENABLE_DEBUG_LOGS` (`true`) — serial output: ROM load messages, `build_rom_list` count, the live
  FPS/timing line.
- `ENABLE_FRAMEBUFFER_TEST` — fills the screen solid green every loop; isolates display wiring.
- `ENABLE_AUDIO` (`true`) — the whole I2S audio path + task; `false` = exactly the pre-audio firmware.
- `ENABLE_AUDIO_TEST` — a raw 440 Hz I2S tone, no APU (proves the amp/speaker before the synth).
- `ENABLE_APU_TEST_NOTE` — a hardcoded APU blip at boot, no ROM/syscall (proves the synth path).
- On a `ppu` build, `PPU_TFT_SELFTEST` runs a standalone color-bar test (proves the panel before any
  command crosses the wire); on a `cpu` build, `CPU_WIRE_LOOPBACK` / `CPU_TESTPATTERN` run the SPI
  loopback / a hand-built test frame instead of the emulator.

## Build & flash

```bash
make flash       # single-chip build -> upload (pio run -t upload, default env = single)
make uploadfs    # push firmware/data/*.rom (+ .pak) to LittleFS
make monitor     # serial monitor @115200

# two-chip roles (connect one board at a time, or pass UPLOAD_PORT=COMx):
make flash-cpu   make flash-ppu       # flash the game chip / the display chip
make mon-cpu     make mon-ppu
```

Drop a compiled `.rom` into `firmware/data/`, `make uploadfs`, and it appears in the menu's ROM list
on next boot (or immediately, since `build_rom_list()` re-mounts and re-scans LittleFS on the
firmware's own boot, not just at flash time). For a two-chip setup the ROMs + `.pak`s live on the
**`cpu`** board (it owns storage); the `ppu` board holds no game data.
