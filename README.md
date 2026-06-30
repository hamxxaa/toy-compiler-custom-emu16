# EMU16

A homebrew game platform built from scratch — a custom 16-bit CPU, a C-like compiler that targets it,
an ESP32-S3 handheld, and a browser simulator, all in one repo. Write a program once; run it on
hardware, in the terminal, or in a browser tab.

| Layer | What it is |
|---|---|
| **Language** | C-like: types, **structs**, **classes**, arrays, pointers, strings, bitwise/shift, `if`/`else`, `while`/`for`, **`switch`** (O(1) jump table), `const`, inline asm |
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
| Data | `0x0008`–`0x3FFF` | globals, arrays, the bitmap font (baked into the ROM), and streamed sprite sheets |
| Code | `0x4000`–`0xADFB` | compiled program |
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

`VRAM_START`, `INPUT_ADDR`, `PRAM`, `SCREEN_WIDTH`, `SCREEN_HEIGHT` are predefined. For the full grammar
see `src/parser/Parser.py`; for real programs see `examples/` and `lib/`.

## Libraries (`lib/`)

- **`io.lib`** — graphics, written in the language itself: `plot`, `fill`, `fill_rect` (clipped box
  fill — the dirty-rectangle erase primitive), `set_palette`, `read_input`, `peek_byte`/`poke_byte`,
  `draw_sprite`, `draw_char`/`draw_string` (text rides on `draw_sprite`). The 8×8 font is a baked-in
  array, so it ships inside every ROM that includes the library. Turn ASCII art into sprite byte
  arrays with `tools/sprite.py`.
- **`sys.lib`** — wrappers for the host syscalls below.
- **`game.lib`** — game helpers: `rand`/`srand`/`rand_range` (xorshift PRNG) and `aabb` box collision.
  Pulls in `sys.lib` for `srand_time()`.
- **`text.lib`** — text + dialog on top of `io.lib`: `strlen`, `draw_text` (NUL-terminated strings),
  `int_to_str`/`draw_number`, `draw_text_box`, `draw_wrapped` (word-wrap), `draw_text_reveal` (typewriter).
- **`anim.lib`** — the sprite/animation runtime (v1). A **clip registry** (`clip_w/h/count/period/buf[]`,
  generated into `sprites.gen.txt` by the importer and indexed by asset id) + a **bump allocator**
  (`anim_load` streams a sheet into a shared pool, `anim_scene_reset` reclaims it) + **time-based**
  blitting (`clip_blit`, `clip_frame` — `frame = (elapsed/period) % count`, so animation speed is
  render-rate-independent) + a thin compile-time **`Animator`** class (`play`/`play_once`/`tick`/`draw`,
  holds only the current clip + start tick + a one-shot lock). Selection stays hand-written per
  character; swarms use struct-arrays over the same registry. The `Animator` also does
  **dirty-rectangle erase**: it remembers the box it last drew, and `erase()` restores the
  background under it (via `fill_rect`) so a frame only repaints what moved — run `erase()` for every
  entity, then `draw()` for every entity (the draw order is the z-order). Built on `io.lib`'s
  `draw_sprite_color`.
- **`scene.lib`** — the top-down **scene engine** for worlds larger than the screen. Owns a
  world-space **object cast** (`struct Obj`/`objs[]`, filled via `spawn_obj`), a **dead-zone follow
  camera** (`camera_follow`, clamped to the world), and a per-frame **render pipeline**
  (`scene_render`) that culls, dirty-rect-erases to a solid ground colour, **Y-sorts by the feet**,
  and clipped-blits. Because struct v1 has no struct pointers, the lib declares the cast/camera
  globals and the game fills them — one cast + one camera per program. It also has **collision v1**:
  per-object **footprint colliders** + a `solid` flag, `move_obj(i, dx, dy)` (axis-separated
  move-and-test → blocking **and** free wall-sliding, world-clamped), and `obj_overlap` (touch). Brute
  force over movers × solids — cheap at this scale; a spatial grid is a noted future. And **combat v1**:
  `hp`/`active`/`blink` on `Obj`, `obj_overlap_box` (collider vs an arbitrary box — attack hitboxes /
  triggers), and `despawn` (lifecycle; render + collision skip inactive). `examples/arena.txt` is the
  reference game (roam + landmarks + solid trees + enemies with HP, an attack hitbox, death/respawn,
  player i-frames, and blink/knockback/flash hit feedback), plus a **talking NPC** — the hasta-ninja
  (press B to interact → a flag-gated one-time line, then a repeatable one) — that exercises the event
  foundation end-to-end. Solid ground only; a tilemap is a future additive layer that would only change
  the erase's background-restore. It also hosts the **event
  triggers** over the cast: `interact_front(i, dir, facing, reach)` ("what am I facing" → the object
  in front, or -1), edge-triggered **zones** (`add_zone` / `zone_entered` — fire once on entry, for
  warps/encounters/tripwires), and `scene_refresh` (full repaint after a UI overlay closes or a map
  loads).
- **`event.lib`** — the **event foundation** (state + UI; scene-independent, depends on `text.lib` +
  `sys.lib`): bit-packed **flags** (`flag_get/set/clear`, 256 bits, `flags_save`/`flags_load`) — the
  spine of save state and the gate for one-time events / branching dialog — and a blocking **dialog
  box** (`dialog_open`/`dialog_update`/`dialog_active`, styled via `dialog_style`) built on
  `draw_text_box` + `draw_wrapped`. Events themselves are **game functions dispatched by a `switch`**
  (the O(1) jump table) on the object/zone id; `tests/test_events.txt` shows the full pattern
  (interact/zones → switch → flags + dialog).

`io.lib` also has the **color sprite + palette** layer: `draw_sprite_color` / `draw_sprite_color_flipx`
(byte-per-pixel, index 0 = transparent, free horizontal mirror), `draw_sprite_color_clipped[_flipx]`
(screen-clamped, for scrolling worlds), `load_palette` (bulk swap), `palette_fill`/`palette_cycle`
(flash, color-cycling effects).

**Sprite pipeline:** draw in [LibreSprite](https://libresprite.github.io/) (indexed mode), export an
indexed PNG + JSON, and list your assets in a `sprites.list`. `tools/image_import.py` (stdlib-only PNG
reader in `tools/png.py`) slices each tagged sheet into the pack manifest + `const` ids + the clip
registry, and `tools/pack_assets.py` builds the `.pak`.

One `sprites.list` row is the **master palette** — a `.gpl` (GIMP/LibreSprite palette, the editable
source of truth) or an indexed PNG (its embedded palette) — which becomes the 256-entry **PRAM**,
loaded once at startup via `sys_asset_load(PAL_MASTER, PRAM, 512)`. Every sprite is byte-per-pixel
**indices into that one shared palette**, so the color at each index is fixed across all sheets:
**append** new colors to the master (line order = index = PRAM order), never reorder or insert. Load
the same `.gpl` into every sprite in the editor so they stay in sync. See [FORMATS.md](FORMATS.md).

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
| 7 / 8 / 9 | `sys_save` · `sys_load` · `sys_save_exists` | persist/restore a per-game save slot |
| 10 / 11 | `sys_asset_info` · `sys_asset_load` | stream sprites/data from the game's `.pak` by id |
| 12 | `sys_set_fps(n)` | request a frame-rate cap (0 = default 60); paces the frame and sizes its instruction budget |

For games, `sys_present()` is the one to know: call it once per frame after drawing and the loop runs
exactly once per displayed frame — no flicker, frame-paced movement.

`examples/menu.txt` is the home screen: an ordinary ROM that lists games through these syscalls. The
firmware boots `/menu.rom`; drop a `.rom` into `firmware/data/`, `make uploadfs`, and it shows up.

## File formats

Three on-disk formats — the program, its assets, and saves:

- **`.rom`** — the compiled program: a flat image of `0x0000…code_end` loaded verbatim at address 0
  (bootstrap → DATA → CODE). Carries no art and no save state.
- **`.pak`** — the asset pack (EPAK): sprites/palettes/text/data bundled by `tools/pack_assets.py`, kept
  in storage and streamed by id via `sys_asset_load` (so a multi-MB pack works on a 64 KB machine). One
  per ROM — `game.pak` beside `game.rom`.
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
lib/         io.lib, sys.lib, game.lib, text.lib, anim.lib, scene.lib (world/camera engine), event.lib (flags + dialog)
tools/       sprite.py (ASCII->array) · png.py + image_import.py (LibreSprite->assets) · pack_assets.py (.pak)
examples/    sample programs (menu.txt, block_blast.txt; walkdemo.txt shows the sprite library; arena.txt is the scene-engine reference: roam + collision + combat + a talking NPC on the event foundation)
tests/       regression sources      main.py · run_tests.py · Makefile · FORMATS.md
```

## Requirements

Python 3.7+ · GCC/MinGW (pc_emu) · Node (WASM verify) · Emscripten (WASM) · PlatformIO (firmware)
