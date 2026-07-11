# EMU16

A homebrew game platform built from scratch — a custom 16-bit CPU, a C-like compiler that targets it,
a **tile/sprite PPU**, an ESP32-S3 handheld, and a browser simulator, all in one repo. Write a program
once; run it on hardware, in the terminal, or in a browser tab.

| Layer | What it is |
|---|---|
| **Language** | C-like: types, **structs**, **classes**, arrays, pointers, strings, bitwise/shift, `if`/`else`, `while`/`for`, **`switch`** (O(1) jump table), `const`, inline asm |
| **Compiler** | Hand-written: regex-NFA lexer → recursive-descent parser → TAC IR → linear-scan allocator → `.rom` |
| **CPU** | Custom 16-bit, 8 registers (R6 = FP, R7 = SP), 64 KB little-endian address space |
| **PPU** | Separate NES-2C02-style **tile + sprite + text** unit with its own graphics RAM; the CPU submits a command buffer, the PPU composes the frame |
| **Targets** | ESP32-S3 handheld · desktop emulator (`pc_emu`) · browser WebAssembly sim |

```bash
python main.py examples/arena.txt --save-rom arena   # compile -> build/roms/arena.rom
pc_emu.exe --rom build/roms/arena.rom --frames 60    # run on PC -> build/pc_emulator/frame.ppm
make flash && make uploadfs                           # flash the handheld
```

(Graphical games stream their art from a `.pak` — see [Assets](#assets--the-sprite-pipeline).)

## Memory map

The CPU has its own 64 KB space; the PPU has a **separate** graphics RAM it alone owns (below).

### CPU address space

| Region | Range | Purpose |
|---|---|---|
| Data | `0x0008`–`0x3FFF` | globals, arrays, the bitmap font, resident map/collision data |
| Code | `0x4000`–`0xADFB` | compiled program (stack grows down into the top of this region) |
| Stack | `0xADFC` down | call stack (pre-decrement) |
| SYSCALL_PORT | `0xADFE` | write-triggered host call (see [Syscalls](#syscalls)) |
| INPUT | `0xADFF` | button state (read-only) |
| PRAM / VRAM | `0xAE00`–`0xFFFF` | **legacy** palette + framebuffer, used only by ROMs that don't drive the PPU |

Calling convention: args in R1–R3, return value in R0, R4–R5 callee-saved.

### PPU graphics RAM (separate from the CPU)

The PPU owns ~56 KB of its own memory, a flat byte array with fixed region offsets. The CPU never
reads it directly — it pushes commands/data in (invariant: the PPU never touches CPU memory).

| Region | Offset | Contents |
|---|---|---|
| PAT | `0x0000` | 128 × 256 B patterns (16×16 tiles **and** sprites share these slots) |
| FONT | `0x8000` | 128 × 8 B 1-bit glyphs |
| TILEMAP | `0x8400` | 32×32 background tile ids (a scrollable torus) |
| TEXTMAP | `0x8800` | 20×16 cells × 3 B `{glyph, fg, bg}` (the HUD/dialog text plane) |
| OAM | `0x8BC0` | 64 sprites × 6 B `{pat, x:i16, y:i16, attr}` |
| PAL | `0x8D40` | 256 × RGB565 |
| REGS | `0x8F40` | scroll_x/y, ctrl, oam_count |
| FB | `0x8F50` | 160×128 indexed framebuffer (converted through PAL at present) |

## Graphics — the PPU

The CPU does **no per-pixel drawing**. Each frame it builds a small command buffer and hands it to the
PPU with `sys_ppu_submit`; bulk art (tilesets, sprite sheets, palettes) is streamed straight into PPU
RAM with `sys_ppu_dma` (from the `.pak`) or `sys_ppu_upload` (from a CPU buffer). The PPU then composes
the frame in NES order: **background tilemap → sprites → text plane**, and converts its indexed
framebuffer through the 256-color palette on `PRESENT`.

- **Background:** a 32×32 tilemap of pattern ids, scrolled as a torus (window-relative 0–511) — the
  camera can roam a world far larger than the window by streaming the leading edge.
- **Sprites:** up to 64 16×16 entries in OAM, with per-sprite horizontal/vertical flip and a
  behind-background priority bit; index 0 is transparent. The CPU decides on-screen order (Y-sort).
- **Text plane:** a 20×16 grid of `{glyph, fg, bg}` cells over the whole screen — the HUD and dialog
  boxes. `bg = 255` is transparent (only the glyph's lit pixels draw, e.g. HUD over the world); any
  other `bg` makes the whole cell opaque (dialog panels).
- **Collision** stays CPU-side: a resident tile grid + a `tile_flags` property table (separate from
  the PPU's tilemap), queried with `ppu_solid_at`.

This models the NES 2C02 (a software PPU, so no NES hardware limits); a future revision splits it onto a
second chip over SPI — the same command bytes work either way.

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
                x = x + 1;
            }
            x = x + 1;
        }
        var int p = &x;                     // pointers: & address-of, * dereference
        return *p;
    }
}
```

- Types: `int` (16-bit), `byte` (1 byte in arrays), `bool`, `void`, and **`struct`** (below). Hex
  literals (`0xB000`) are fine; string literals (`"hi\n"`) are NUL-terminated byte arrays whose value is
  the address (escapes `\n \t \\ \" \0`).
- Operators: `+ - * / %`, unary `-`, `& | ^ ~`, `<< >>`, comparisons, `&&`/`||` (parenthesize each side).
  Precedence loosest→tightest: bitwise < shift < `+ -` < `* / %`; comparisons bind loosest. There is no `!`.
- Control flow: `if`/`else`, `while`, `for (init; cond; post)`, `break`, `continue`, and `switch`
  (over compile-time-constant `case`s, auto-break/no fallthrough; compiles to an **O(1) jump table**).
- `const NAME = <expr>;` — compile-time integer constants; they fold to literals and may be used as
  array sizes (`const N = 64; var int a[N];`).
- `struct Name { int a; byte b; }` then `var Name s;` or `var Name arr[N];`; read/write with `s.a` /
  `arr[i].b`. Data-only and byte-packed. Pass entities via a global array + an index — struct
  pointers/params aren't supported yet.
- `class Name { … }` — compile-time **objects**: `new Name obj;` stamps out a uniquely-named copy of the
  class's fields + methods (monomorphization); call them with `obj.method()`, access fields inside a
  method via `self.field`. Supports composition (`var Other sub;` → `self.sub.method()`) and a manual
  `init()`. v1 limits: static/global named instances; fields must be primitive or a composed class
  (big buffers stay global); no inheritance; no runtime-indexed object arrays — use struct-arrays for
  swarms. (Desugars to plain globals + functions before analysis, so it's pure front-end sugar.)
- A function whose whole body is one `asm { }` block is **naked**: no prologue, args in R1–R3, return
  in R0, you write `RET`. The IO and syscall libraries are built this way.
- `include "path";` splices a library's source into the program — there is no separate linker.

`SCREEN_WIDTH`, `SCREEN_HEIGHT`, and the syscall/PPU library constants are predefined. For the full
grammar see `src/parser/Parser.py`; for real programs see `examples/` and `lib/`.

## Libraries (`lib/`)

- **`ppu.lib`** — the graphics interface. Builds a PPU command buffer and flushes it: `cmd_reset` →
  emit commands → `ppu_present` (submits + yields the frame). Covers **scroll** (`ppu_scroll`,
  `ppu_backdrop`), **sprites** (an OAM shadow via `oam_clear`/`oam_set(pat,x,y,attr)`), the **text
  plane** (`tile_text`, `tile_text_bg` opaque, `tile_fill`/`tile_clear`, `tile_number`), **asset
  streaming** (`sys_ppu_dma`/`sys_ppu_upload`, `ppu_upload_font`), and **CPU-side tile collision**
  (`ppu_solid_at(cmap, map_w, flags, wx, wy)` against a resident grid + `tile_flags`). Region constants
  (`PPU_PAT`, `PPU_FONT`, `PPU_TILEMAP`, `PPU_PAL`) and glyph sentinels (`GLYPH_SOLID`, `GLYPH_BLANK`,
  `PPU_NO_BG`) live here.
- **`ppuscene.lib`** — a small top-down **scene layer** on top of `ppu.lib`. A **dead-zone follow
  camera** (`scene_follow`, clamped to the world), **tilemap streaming** (`scene_stream` pushes the
  visible window of a world larger than the 32×32 tilemap and sets the scroll), and a world-space
  **Y-sorted sprite cast** (`scene_clear`/`scene_obj`/`scene_draw` — nearer objects drawn in front,
  converted world→screen with the camera and emitted as OAM).
- **`io.lib`** — minimal CPU-side I/O: `read_input` + edge helpers (`button`, `button_pressed`,
  `button_released`), `peek`/`poke`(`_byte`), the baked-in 8×8 `font8x8` array (uploaded to the PPU
  font region), and `draw_row8` (a low-level VRAM row primitive kept for the legacy display path).
- **`sys.lib`** — wrappers for the host syscalls below.
- **`game.lib`** — `rand`/`srand`/`rand_range` (xorshift PRNG) and `aabb` box collision; pulls in
  `sys.lib` for `srand_time()`.
- **`event.lib`** — the **event/state spine**: bit-packed **flags** (`flag_get`/`set`/`clear`,
  `flags_reset`, 256 bits, `flags_save`/`flags_load` over a save slot) — the save state and the gate
  for one-time events / branching dialog. Events themselves are **game functions dispatched by a
  `switch`** on the object/zone id; dialog is drawn on the PPU text plane (`tile_text_bg`).

## Syscalls

A ROM asks the host to do something it can't — list/load games, drive the PPU, stream assets, pace a
frame — by byte-storing a *syscall number* to `SYSCALL_PORT` (`0xADFE`): args in R1–R3, result in R0.
It must be a byte store; a word write would also hit INPUT at `0xADFF`. Each host registers its own
handler (firmware over LittleFS, `pc_emu` over `build/roms/`, the browser over dropped ROMs + `.pak`s).

| # | Wrapper | Does |
|---|---|---|
| 1 / 2 | `sys_list_roms` · `sys_get_rom_name` | enumerate the available ROMs |
| 3 / 4 | `sys_load_rom` · `sys_reset` | launch a game · return to the menu |
| 5 | `sys_present()` | display the finished frame, pace to ~60 Hz, then resume — a vblank |
| 6 | `sys_time()` | milliseconds since boot (low 16 bits) |
| 7 / 8 / 9 | `sys_save` · `sys_load` · `sys_save_exists` | persist/restore a per-game save slot |
| 10 / 11 | `sys_asset_info` · `sys_asset_load` | read a pak asset's header · copy it into CPU memory |
| 12 | `sys_set_fps(n)` | request a frame-rate cap (0 = default 60); paces the frame + sizes its instruction budget |
| 13 | `sys_ppu_submit(buf, len)` | execute a PPU command stream; a trailing `PRESENT` yields the frame |
| 14 | `sys_ppu_dma(pak_id, ppu_addr)` | stream a pak asset straight into PPU RAM (tilesets, sheets, palette) |
| 15 | `sys_ppu_upload(ppu_addr, cpu_src, len)` | copy a CPU buffer into PPU RAM (baked data) |

For PPU games the loop is: `cmd_reset` → build commands (scroll, OAM, text) → `ppu_present()` (which
submits and yields), so the loop runs exactly once per displayed frame — frame-paced, no tearing.

`examples/menu.txt` is the home screen: an ordinary ROM that lists games through syscalls 1–4. The
firmware boots `/menu.rom`; drop a `.rom` into `firmware/data/`, `make uploadfs`, and it shows up.

## Assets — the sprite pipeline

Draw in [LibreSprite](https://libresprite.github.io/) (indexed mode), export an indexed PNG + JSON, and
list your assets in a `sprites.list`. `tools/image_import.py` (stdlib-only PNG reader in `tools/png.py`)
slices each tagged sheet into a pack manifest + `const` ids + a clip registry; `tools/pack_assets.py`
builds the `.pak` (EPAK). At runtime the game **DMAs** patterns into PPU pattern slots and the palette
into PPU PAL (`sys_ppu_dma`).

One `sprites.list` row is the **master palette** — a `.gpl` (GIMP/LibreSprite palette, the editable
source of truth) or an indexed PNG — which becomes the 256-entry PPU palette. Every sprite is
byte-per-pixel **indices into that one shared palette**, so a color at a given index is fixed across all
sheets: **append** new colors to the master (line order = index), never reorder or insert.

Paint **tilemaps** with `tools/pixel_map.py`: an indexed PNG where each pixel is one 16×16 tile, plus a
legend mapping palette index → tile id + solidity → a generated `world[]` + `tile_flags[]` include (the
tilemap doubles as the collision grid). Turn ASCII art into a sprite byte array with `tools/sprite.py`.
See [FORMATS.md](FORMATS.md).

## File formats

- **`.rom`** — the compiled program: a flat image of `0x0000…code_end` loaded verbatim at address 0
  (bootstrap → DATA → CODE). Carries no art and no save state.
- **`.pak`** — the asset pack (EPAK): patterns/palettes/tilemaps/text/data bundled by
  `tools/pack_assets.py`, kept in storage and streamed by id (so a multi-MB pack works on a 64 KB
  machine). One per ROM — `game.pak` beside `game.rom`.
- **saves** — opaque per-game, per-slot blobs written/read via `sys_save`/`sys_load`.

Full byte layouts, the load/stream path, and the per-platform storage map are in **[FORMATS.md](FORMATS.md)**.

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
the browser all run the **same `emu.cpp` + `ppu.cpp` core** compiled three ways — one ISA, no drift.

The browser sim (drop a `.rom`, plus its `.pak` for art) has an **FPS cap** and a **CPU-budget slider**
(instructions/frame — dial it down to feel a weaker device), live **FPS / instr-per-sec / instr-per-frame**
stats, and **CPU + PPU memory viewers**.

## Firmware (ESP32-S3)

A PlatformIO project under `firmware/` (open that folder, not the repo root). `emu.cpp` + `ppu.cpp` are
pulled in via `src/emu_shim.cpp` — no second copy. Pins live in `src/hw_pins.h`, build flags in
`platformio.ini`.

```bash
make flash       # build + upload the firmware
make uploadfs    # upload firmware/data/*.rom to LittleFS
```

Toggles in `src/main.cpp`: `ENABLE_DEBUG_LOGS` (serial output) and `ENABLE_FRAMEBUFFER_TEST` (a
solid-color panel check). Once a ROM engages the PPU the firmware converts its composed frame and pushes
it via `tft.pushImage`; the per-frame instruction budget is in `emu.cpp`'s `run_frame_instructions`.

## Layout

```
src/         compiler: lexer · parser · analyzer · codegen · optimization · backend
emulator/    emu.cpp (CPU core) + ppu.cpp (PPU) + definitions.h, the PC harness, the WASM bridge
firmware/    ESP32-S3 PlatformIO project (main.cpp, hw_pins.h, data/ ROMs)
simulator/   browser sim — HTML/JS over the WASM core (display + CPU/PPU debug panels)
lib/         ppu.lib · ppuscene.lib · io.lib · sys.lib · game.lib · event.lib
tools/       sprite.py (ASCII->array) · png.py + image_import.py (LibreSprite->assets) · pack_assets.py (.pak) · pixel_map.py (tilemaps)
examples/    arena.txt (PPU scene reference: roam a tiled world + collision + combat + a talking NPC), ppu_overworld.txt / ppu_bigworld.txt (PPU + streaming demos), menu.txt (home screen)
tests/       regression sources      main.py · run_tests.py · Makefile · FORMATS.md
```

## Requirements

Python 3.7+ · GCC/MinGW (pc_emu) · Node (WASM verify) · Emscripten (WASM) · PlatformIO (firmware)
