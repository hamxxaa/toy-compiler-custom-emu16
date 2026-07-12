# Browser simulator

`simulator/` is a browser-based, no-install dev target: drop a `.rom` (and its `.pak`, if it has
one) onto a page and it runs — the same WASM build of `emu.cpp`+`ppu.cpp` as every other host (see
[Architecture](architecture.md#the-shared-core)). Its purpose is fast iteration and visibility
(memory viewers, live stats), not being a "real" deployment target.

## Running it

```bash
make wasm                              # builds simulator/emu.js + simulator/emu.wasm
cd simulator && python -m http.server   # MUST be served over http:// — file:// breaks WASM fetch
```

Then open the printed `localhost` URL and drop a ROM in.

## Build

`make wasm` (or `simulator/build.sh` directly) compiles `emulator/emu.cpp` + `emulator/ppu.cpp` +
`emulator/emu_wasm.cpp` with Emscripten:

```
emcc emulator/emu.cpp emulator/ppu.cpp emulator/emu_wasm.cpp \
     -O2 -DEMU_COUNT_INSTRUCTIONS -s MODULARIZE=1 -s EXPORT_NAME=createEmu \
     -s "EXPORTED_RUNTIME_METHODS=['cwrap','ccall','HEAPU8']" \
     -s ALLOW_MEMORY_GROWTH=0 -s INITIAL_MEMORY=16777216 -s ENVIRONMENT=web,node \
     -o simulator/emu.js
```

Keep `Makefile`'s `EMCC_FLAGS` and `simulator/build.sh` in sync — they're two independent build
entry points for the same output. `-DEMU_COUNT_INSTRUCTIONS` is set here (unlike the firmware build)
because the simulator is a dev tool where the executed-instruction counter (live instructions/sec,
instructions/frame) is worth its negligible per-instruction cost; firmware omits it for zero
production overhead. `ALLOW_MEMORY_GROWTH=0` + a fixed 16 MB `INITIAL_MEMORY` means the guest 64 KB
memory's heap pointer never moves, which is what lets `EmuCore.memory` hand out a live `subarray`
view instead of re-reading a pointer every access.

## Architecture

| File | Role |
|---|---|
| `wasm.js` | `EmuCore` — a thin JS class wrapping the `cwrap`'d C exports (`emu_init`, `emu_run_frame`, `emu_reg`, `emu_ppu_*`, `emu_asset_*`, ...). Deliberately mirrors an older hand-ported `cpu.js`'s surface (`memory`, `getRegWord`, `pc`, `flags`, `running`, `runBatch`, `initialize`) so the rest of the frontend needed no changes when the hand-rolled CPU was replaced by the real WASM core. |
| `app.js` | `App` — the main controller: drop-zone / file-input handling (multi-file, so a ROM + its `.pak` can be dropped together), run/pause/step/reset, the FPS-cap `<select>`, the CPU-instruction-budget slider, the deadline-accumulator frame-pacing loop. |
| `display.js` | Canvas rendering of the PPU's converted RGB565 framebuffer (the only display path — the legacy VRAM+PRAM path was reclaimed). |
| `debug.js` | `DebugPanel` — CPU register/flag/PC display, the CPU memory hex viewer, and the PPU memory hex viewer (`#mem-viewer`/`#ppu-mem-viewer`, jump-to-address inputs) plus PPU state (scroll x/y, OAM count) decoded from the PPU's REGS region. |
| `input.js` | Keyboard + on-screen d-pad/action-button input, written into the guest `INPUT` byte each frame. |

`EmuCore` binds every `cwrap` optimistically via `_opt()`: if a given export is missing (a stale
cached `emu.wasm` built before that export existed), the corresponding method degrades to a stub
returning 0 instead of throwing — so an old cached build shows a black PPU screen instead of
crashing the whole page. **Hard-refresh if the PPU output looks wrong after a rebuild** — it's almost
always a stale cached `.wasm`, not a real bug.

## Features

- **FPS cap** — Auto / 30 / 60 / 120 / uncapped, backed by a deadline-accumulator pacing loop that
  stays correct under `requestAnimationFrame` jitter (rather than assuming every RAF tick is exactly
  16.7 ms).
- **CPU-budget slider** — instructions-per-frame, exposed so you can dial down a weaker "device"
  and see how a game degrades, independent of the FPS cap.
- **Live stats** — FPS, instructions/sec, instructions/frame (needs the WASM build's
  `-DEMU_COUNT_INSTRUCTIONS`, see above).
- **CPU + PPU memory viewers** — hex dumps of guest memory and PPU graphics RAM, each with a
  jump-to-address field, live-updating while the emulator runs.
- **Multi-file drop** — drop a ROM and its `.pak` together (or one at a time); an empty/missing
  palette after drop surfaces a warning rather than silently rendering black.
- **Per-game saves** — `localStorage`, namespaced `emu16:<rom>.<slot>` via `EmuCore.setCurrentRom()`.
  See [file-formats.md](file-formats.md#4-save-files).
- **Asset pack bridge** — `EmuCore.setAssetPack(bytes)` copies a dropped `.pak`'s bytes into the
  WASM heap and commits it, so `sys_asset_info`/`sys_asset_load`/`sys_ppu_dma` work exactly as they
  would on the other two hosts.

## Debugging tips

- Console/network errors, if the page misbehaves, are the first thing to check — this is a normal
  web page, ordinary browser devtools apply.
- A black screen with a loaded ROM almost always means either: no matching `.pak` was dropped for a
  PPU game that needs one (watch for the empty-palette warning), or a stale cached `emu.wasm` (hard
  refresh).
- The CPU/PPU memory viewers are the fastest way to confirm whether a `sys_ppu_dma`/`sys_ppu_upload`
  actually landed where you expect — jump the PPU viewer to the region in question (see
  [memory-map.md](memory-map.md#ppu-graphics-ram) for offsets) and look for the expected bytes.
