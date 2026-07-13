# Desktop emulator (`pc_emu`)

`pc_emu.exe` is a native, one-shot CLI harness around the shared `emu.cpp`+`ppu.cpp`+`apu.cpp` core
(`emulator/pc_emulator_main.cpp`): load a ROM, run a fixed number of frames, write the final
framebuffer to a `.ppm` and the rendered audio to a `.wav`, print a summary line. No display window,
no interactivity, no real-time pacing — it exists for scripted regression testing (`run_tests.py`)
and for headless debugging, not for playing games.

## Build

```bash
make pc_emu
# equivalent to:
g++ -std=c++17 -O2 -static -DEMU_COUNT_INSTRUCTIONS \
    emulator/emu.cpp emulator/ppu.cpp emulator/apu.cpp emulator/pc_emulator_main.cpp -o pc_emu.exe
```

`-DEMU_COUNT_INSTRUCTIONS` is set for this build only (a desktop dev/metrics build) — firmware and
the production WASM path omit it for zero overhead; the simulator's WASM build re-enables it
separately since it's also a dev tool (see [simulator.md](simulator.md#build)).

## CLI

```
pc_emu.exe --rom <path> [--output-dir <dir>] [--frames <count>] [--menu] [--hold-input N] [--help]
```

| Flag | Default | Meaning |
|---|---|---|
| `--rom <path>` | *(required)* | ROM to load. May also be given as a bare positional argument. |
| `--output-dir <dir>` | `build/pc_emulator` | Where `frame.ppm` is written. |
| `--frames <count>` | 1 | Run this many display frames before stopping. |
| `--menu` | off | Register the ROM-listing/loading syscall cases (1-3) against `build/roms/*.rom`, mimicking the firmware's boot-menu behavior. Without it, cases 1-3 are no-ops (empty list) — fine for a single-ROM headless test, not for exercising `examples/menu.txt` itself. |
| `--hold-input N` | none | Force the guest `INPUT` byte to `N` (accepts `0x`-prefixed hex) every frame — a headless stand-in for holding a button, e.g. `--hold-input 4` = RIGHT held, useful for proving a camera scrolls without a real input device. |
| `--help` / `-h` | | Print usage and exit. |

The syscall handler (`pc_syscall_handler` in `pc_emulator_main.cpp`) is registered **unconditionally**
(not only when `--menu` is passed), so save/load/asset syscalls always work in headless tests — only
the ROM-list-specific cases (1-4) are gated behind `--menu`. It additionally implements syscall
`0x7F` (ECHO: `R0 = R1 + R2`), a PC-only diagnostic that exists solely so
`tests/test_syscall_echo.txt` can regression-test the full port-write → handler → arg-read →
result-write path — see [syscalls.md](syscalls.md#notes--per-host-differences).

Per-ROM save/asset namespacing mirrors the firmware: the ROM's file basename (no extension) becomes
`g_pc_current_rom`, used for `saves/<rom>.<slot>` and to locate `<rom-dir>/<rom>.pak` (loaded lazily,
once, on first asset syscall — see [file-formats.md](file-formats.md#2-pak--the-asset-pack-epak)).

## Output

Three things every run produces:

1. **`<output-dir>/frame.ppm`** — the PPU's composed frame (every ROM drives the PPU now), a plain
   P6 PPM (`SCREEN_WIDTH`×`SCREEN_HEIGHT`, RGB565 expanded to 8-bit-per-channel). Viewable in any
   image tool that reads PPM, or convertible with `magick`/`convert`.
2. **`<output-dir>/apu.wav`** — the whole run's audio, rendered offline. There's no real-time clock
   here, so each completed game frame renders exactly `apu_rate/60` samples (assuming the project's
   default `apu_fps=60`; `sys_set_fps` is a no-op in this runner) — `22050/60` isn't an integer
   (367.5), so a remainder accumulator alternates 367/368 samples per frame to average out exactly.
   Bit-identical to the same ROM's audio on every other host, by the same integer-determinism
   guarantee as the framebuffer checksum below. This is what the `song_render`/`mml.py --preview`
   fast-iteration path cross-checks against (see [tools.md](tools.md#mmlpy)).
3. **Two summary lines on stdout:**
   ```
   REGS R0=0x01AB R1=0x01AB R2=0x01AB R3=0x80B2 R4=0x0000 R5=0x0000 R6=0x0000 R7=0xFFFC
   RESULT halted=1 frames=1 batches=1 return=427 pc=0x8026 flags=0x0000 instr=292 checksum=0x7A83BC47B8448383 frame=build\pc_emulator\frame.ppm audio=build\pc_emulator\apu.wav (367 samples)
   ```
   - `REGS` — all 8 registers' final values, hex.
   - `halted` — 1 if the CPU stopped on a genuine `HLT` (not a `PRESENT` frame-yield) before
     `--frames` completed; 0 if it ran the full frame count (or is still mid-`PRESENT`-cycling).
   - `frames` / `batches` — completed display frames / instruction-execution batches (normally equal;
     they can differ if a ROM halts mid-run).
   - `return` — `R0` at the end, by convention a test's/program's return value.
   - `pc`, `flags` — final program counter and flags register.
   - `instr` — total executed-instruction count (needs `-DEMU_COUNT_INSTRUCTIONS`, always on for this
     build).
   - `checksum` — FNV-1a 64-bit hash of the final framebuffer (every RGB565 pixel, low byte then high
     byte). A **deterministic** proxy for "the screen looks right" without needing pixel-level image
     comparison or a human to look at anything — printed for manual side-by-side diffing during
     development (e.g. comparing a `pc_emu` run against the same ROM's WASM output by hand), but
     `run_tests.py`/`verify_wasm.js` don't assert on it automatically (see below for what they do
     check).
   - `frame` — the `.ppm` path.
   - `audio` — the `.wav` path and total sample count.

This is the mechanism the whole regression suite is built on: every `tests/*.txt` has a known-good
`return` value, and the ROM computes that value **itself** — usually by folding some internal state
into one number (a byte-sum-and-length checksum of a command buffer it just built, a bitmask where
each bit is one passing assertion, etc. — see e.g. `test_ppu_backdrop.txt`, `test_flags.txt`,
`test_apu_cmdstream.txt`). `run_tests.py` compiles each test, runs it through `pc_emu.exe`, and
compares only that `return` value against the expected constant hard-coded next to it in
`run_tests.py`'s own `TESTS` list. `verify_wasm.js` (`node verify_wasm.js`) runs the same ROMs through
the browser's WASM core instead and checks the identical `return` values — proving the WASM and
native builds agree, which is the strongest evidence the hosts haven't drifted. Neither script reads
or compares the framebuffer checksum or the rendered audio.

## Not implemented here

- Real-time frame pacing (`sys_set_fps` is a no-op — the runner just executes `--frames` worth of
  frames back to back as fast as possible).
- Interactive input beyond `--hold-input`'s static byte.
- A live display — you always get a single final `.ppm`, not a frame-by-frame view. For that, use
  the [browser simulator](simulator.md) instead.
