# EMU16

A homebrew game platform built from scratch — a custom 16-bit CPU, a C-like compiler that targets it,
an ESP32-S3 handheld, and a browser simulator, all in one repo. Write a program once; run it on
hardware, in the terminal, or in a browser tab.

| Layer | What it is |
|---|---|
| **Language** | C-like: types, arrays, pointers, bitwise/shift, `if`/`else`, `while`, inline asm |
| **Compiler** | Hand-written: regex-NFA lexer → recursive-descent parser → TAC IR → linear-scan allocator → `.rom` |
| **CPU** | Custom 16-bit, 8 registers (R6 = FP, R7 = SP), 64 KB little-endian address space |
| **Targets** | ESP32-S3 handheld · desktop emulator (`pc_emu`) · browser WebAssembly sim |

```bash
python main.py tests/demo.txt --save-rom demo     # compile -> build/roms/demo.rom
pc_emu.exe --rom build/roms/demo.rom --frames 1   # run on PC
make flash && make uploadfs                        # flash the handheld
```

## Memory map

| Region | Range | Purpose |
|---|---|---|
| Data | `0x0008`–`0x0FFF` | globals, arrays, and the bitmap font (baked into the ROM) |
| Code | `0x1000`–`0xADFB` | compiled program |
| Stack | `0xADFC` down | call stack (pre-decrement) |
| SYSCALL_PORT | `0xADFE` | write-triggered host call (see [Syscalls](#syscalls)) |
| INPUT | `0xADFF` | button state (read-only) |
| PRAM | `0xAE00`–`0xAFFF` | 256-entry RGB565 palette |
| VRAM | `0xB000`–`0xFFFF` | 160×128 indexed framebuffer |

Calling convention: args in R1–R3, return value in R0, R4–R5 callee-saved.

## Language

C-like with a few twists — one example covers most of it:

```c
include "lib/io.lib";
{
    var byte pal[3] = {1, 2, 4};            // arrays; initializer list is baked into the ROM
    int add(int a, int b) { return a + b; }

    int main() {
        var int x = 0;
        while x < SCREEN_WIDTH {             // parens around conditions are optional
            if (x & 1) == 0 {               // & | ^ bind looser than == (unlike C)
                plot(x, 64, pal[1]);
            }
            x = x + 1;
        }
        var int p = &x;                     // pointers: & address-of, * dereference
        return *p;
    }
}
```

- Types: `int` (16-bit), `byte` (1 byte in arrays), `bool`, `void`. Hex literals (`0xB000`) are fine.
- Operators: `+ - * /`, `& | ^ ~`, `<< >>`, comparisons, `&&`/`||` (parenthesize each side). Precedence
  loosest→tightest: bitwise < shift < `+ -` < `* /`; comparisons bind loosest. There is no `!`.
- A function whose whole body is one `asm { }` block is **naked**: no prologue, args in R1–R3, return
  in R0, you write `RET`. The IO and syscall libraries are built this way.
- `include "path";` splices a library's source into the program — there is no separate linker.

`VRAM_START`, `INPUT_ADDR`, `PRAM`, `SCREEN_WIDTH`, `SCREEN_HEIGHT` are predefined. For the full grammar
see `src/parser/Parser.py`; for real programs see `examples/` and `lib/`.

## Libraries (`lib/`)

- **`io.lib`** — graphics, written in the language itself: `plot`, `fill`, `set_palette`, `read_input`,
  `peek_byte`/`poke_byte`, `draw_sprite`, `draw_char`/`draw_string` (text rides on `draw_sprite`). The
  8×8 font is a baked-in array, so it ships inside every ROM that includes the library. Turn ASCII art
  into sprite byte arrays with `tools/sprite.py`.
- **`sys.lib`** — wrappers for the host syscalls below.

## Syscalls

A ROM asks the host to do something it can't — list/load games, pace a frame, read the clock — by
byte-storing a *syscall number* to `SYSCALL_PORT` (`0xADFE`): args in R1–R3, result in R0. It must be a
byte store; a word write would also hit INPUT at `0xADFF`. Each host registers its own handler
(firmware over LittleFS, `pc_emu --menu` over `build/roms/`, the browser over dropped ROMs).

| # | Wrapper | Does |
|---|---|---|
| 1 / 2 | `sys_list_roms` · `sys_get_rom_name` | enumerate the available ROMs |
| 3 / 4 | `sys_load_rom` · `sys_reset` | launch a game · return to the menu |
| 5 | `sys_present()` | display the finished frame, pace to ~60 Hz, then resume — a vblank |
| 6 | `sys_time()` | milliseconds since boot (low 16 bits) |

For games, `sys_present()` is the one to know: call it once per frame after drawing and the loop runs
exactly once per displayed frame — no flicker, frame-paced movement.

`examples/menu.txt` is the home screen: an ordinary ROM that lists games through these syscalls. The
firmware boots `/menu.rom`; drop a `.rom` into `firmware/data/`, `make uploadfs`, and it shows up.

## Building & running

```bash
make pc_emu    # desktop emulator (GCC / MinGW)
make wasm      # browser simulator (Emscripten)
make test      # compile + run the regression suite
make verify    # headless WASM cross-check (Node) — proves the browser core matches pc_emu
make clean

cd simulator && python -m http.server   # browser sim (serve over http://, not file://)
```

`pc_emu.exe` runs a ROM for N frames and writes the final framebuffer to a PPM; its `RESULT` line gives
the return value, executed instruction count, and a checksum. The firmware, the desktop emulator, and
the browser all run the **same `emu.cpp` core** compiled three ways — one ISA, no drift.

## Firmware (ESP32-S3)

A PlatformIO project under `firmware/` (open that folder, not the repo root). `emu.cpp` is pulled in via
`src/emu_shim.cpp` — no second copy. Pins live in `src/hw_pins.h`, build flags in `platformio.ini`.

```bash
make flash       # build + upload the firmware
make uploadfs    # upload firmware/data/*.rom to LittleFS
```

Toggles in `src/main.cpp`: `ENABLE_DEBUG_LOGS` (serial output) and `ENABLE_FRAMEBUFFER_TEST` (a
solid-color panel check). Frames push synchronously via `tft.pushImage`; the per-frame instruction
budget is in `emu.cpp`'s `run_frame_instructions`.

## Layout

```
src/         compiler: lexer · parser · analyzer · codegen · optimization · backend
emulator/    emu.cpp (the one CPU core) + definitions.h, the PC harness, the WASM bridge
firmware/    ESP32-S3 PlatformIO project (main.cpp, hw_pins.h, data/ ROMs)
simulator/   browser sim — HTML/JS over the WASM core
lib/         io.lib, sys.lib
tools/       sprite.py — ASCII art -> sprite byte array
examples/    sample programs (incl. menu.txt, walking.txt)
tests/       regression sources      main.py · run_tests.py · Makefile
```

## Requirements

Python 3.7+ · GCC/MinGW (pc_emu) · Node (WASM verify) · Emscripten (WASM) · PlatformIO (firmware)
