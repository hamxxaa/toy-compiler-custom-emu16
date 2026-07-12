# Memory map

Two separate address spaces: the **CPU's** 64 KB, and the **PPU's** own ~56 KB of graphics RAM. The
CPU never reads or writes the PPU's RAM directly (see [Architecture](architecture.md#ppu)) — every
byte that crosses over does so through an explicit command stream or a bulk copy
(`sys_ppu_submit`/`sys_ppu_dma`/`sys_ppu_upload`).

Source of truth: [`emulator/definitions.h`](../emulator/definitions.h) (shared by all three hosts)
and [`src/backend/EmuBackend.py`](../src/backend/EmuBackend.py) (`CODE_START_ADDRESS`,
`DATA_START_ADDRESS`, `DATA_END_ADDRESS` — compiler-only constants; the emulators just load a flat
ROM image and don't need to know where DATA ends, except for the one stack-underflow sanity check
noted below).

## CPU address space

| Region | Range | Size | Purpose |
|---|---|---|---|
| Bootstrap | `0x0000`–`0x0007` | 8 B | `LDI SP, <stack top>` ; `JMP CODE_START` — the only bytes with a fixed meaning regardless of program |
| DATA | `0x0008`–`0x7FFF` | 32,760 B | globals, arrays, string/array literals — see [below](#whats-actually-in-data) |
| CODE | `0x8000`–`0xFFFB` | ~32.7 KB | compiled instructions (entry block → every function); shares this region with the stack |
| Stack | `0xFFFC` downward | — | call stack, **pre-decrement** (push writes, then decrements `SP`) |
| SYSCALL_PORT | `0xFFFE` | 1 B | write-triggered host call — see [Syscalls](syscalls.md) |
| INPUT | `0xFFFF` | 1 B | button state, read-only from the guest's perspective |

There is no VRAM/PRAM anymore — the legacy indexed-framebuffer display path was reclaimed once every
ROM had been ported to drive the PPU (see [file-formats.md](file-formats.md) history); that freed
~21 KB, split between a bigger DATA section and a bigger CODE+stack region.

`STACK_FLOOR` (`emulator/definitions.h`) and `CODE_START_ADDRESS` (`EmuBackend.py`) are **the same
number by construction** (`0x8000`) — `STACK_FLOOR` is the emulator core's stack-underflow sanity
check (`emu.cpp`: refuse to push below it), and it must equal wherever CODE actually starts, since
the stack grows down from just above CODE's ceiling into unused DATA/CODE headroom... in practice the
stack lives *above* CODE, at `0xFFFC` down to (in principle) `STACK_FLOOR`, giving it roughly 32.7 KB
of headroom shared with CODE's tail. If DATA is ever resized again, **both** constants move together
— that's the one place the boundary is duplicated, and it's checked by `tests/` on every resize.

### What's actually in DATA

DATA is **not** a fixed layout with named slots — it's a flat bump allocator
(`EmuBackend.next_data_address`, starting at `DATA_START_ADDRESS = 0x0008`) that assigns each global
variable and literal (string/array initializers) the next free address, **in the order they're
declared across the fully-merged translation unit**. Since `include` is a textual splice processed
depth-first (see [Architecture](architecture.md#compiler-src)), that order is:

1. The hardware-constant prelude (`SCREEN_WIDTH`, `SCREEN_HEIGHT`, `INPUT_ADDR`) — these are
   `const`s, though, so they fold to literals and take **no DATA space** at all.
2. Each `include`d file's globals, in the order the files were first encountered (depth-first,
   dependencies before dependents) — e.g. for a PPU game including `map.lib` (which itself includes
   `ppu.lib`, which itself includes `sys.lib`) and `scene.lib` (which also includes `ppu.lib`, deduped
   by path), the layout runs roughly:
   - `ppu.lib`'s `font8x8[768]`, `ppu_cmd[2048]` command buffer, `oam_sh[384]` OAM shadow, plus a
     handful of small scratch buffers/counters
   - `map.lib`'s `map_buf[MAX_MAP_BLOB]` — **9,728 bytes**, the single largest thing most PPU games
     put in DATA (see [file-formats.md](file-formats.md#3-map-blobs-a-tilemap-asset)) — if the game
     uses the map system
   - `scene.lib`'s per-object arrays (`SCENE_MAX_OBJ = 32` entries × 4 arrays)
   - `event.lib`'s `flags[32]`, `game.lib`'s RNG state, etc., if included
3. The game's own top-level globals/arrays, last.

There is no way to force a particular global to a particular address, and no padding/alignment
beyond "next free byte" — a `byte` array and an `int` array pack back-to-back with no gap. DATA has
been bumped twice: 16 KB → 24 KB when the map system landed (`map_buf` alone is 9,728 bytes, and it
has to coexist with everything else already declared ahead of it in the include chain), then
24 KB → 32 KB when the legacy VRAM/PRAM path was reclaimed and its freed space was split between a
bigger DATA section and a bigger CODE+stack region.

### Calling convention

- Arguments: `R1`, `R2`, `R3` (in order; a 4th argument has no register slot — none of the current
  libraries need one, so this hasn't come up).
- Return value: `R0`.
- Callee-saved: `R4`, `R5` (a callee that uses them must save/restore, e.g. `sys_save`'s asm body
  does `PSH R4` / ... / `POP R4` when it needs a spare register beyond R1-R3).
- `R6` = frame pointer, `R7` = stack pointer, by convention (not hardware-enforced — the ISA has no
  register that's *only* usable as SP; `R7` just always holds it by compiler convention).
- **Naked functions**: a function whose entire body is one `asm { }` block skips the
  prologue/epilogue — no automatic stack frame, no automatic `RET`. Every library wrapper around a
  syscall is written this way (see `lib/sys.lib`, `lib/ppu.lib`): arguments are already sitting in
  `R1`-`R3` when the asm body starts, so it just needs to not clobber them before it's done, then
  `RET` itself.

### Registers & flags

8 registers (`R0`-`R7`), each a 16-bit word addressable as a whole word or as `lower`/`upper` bytes
(the `reg` union in `emulator/emu.h`). The flags register (`emu.h`: `cpu::flags`) packs **zero**
(bit 0), **sign** (bit 1), and **carry** (bit 2), set by `CMP`/arithmetic and read by the conditional
jumps (`JZ`/`JNZ`/`JS`/`JNS`/`JC`/`JNC`).

## PPU graphics RAM

A separate flat byte array (`ppu.cpp`), **not** part of the CPU's 64 KB. Total size `0xDF50` =
57,168 bytes (≈ 55.8 KB); the exact region sizes below leave **zero gaps** — each region starts
exactly where the previous one ends.

| Region | Offset | Size | End (excl.) | Contents |
|---|---|---|---|---|
| PAT | `0x0000` | 32,768 B (0x8000) | `0x8000` | 128 × 256 B patterns — 16×16 tiles **and** sprites share this one pool of slots |
| FONT | `0x8000` | 1,024 B (0x400) | `0x8400` | 128 × 8 B 1-bit glyphs (ASCII 32-127 at slots 0-95, `GLYPH_SOLID`=96, `GLYPH_BLANK`=97) |
| TILEMAP | `0x8400` | 1,024 B (0x400) | `0x8800` | 32×32 background tile ids — a scrollable torus (window-relative 0-511) |
| TEXTMAP | `0x8800` | 960 B (0x3C0) | `0x8BC0` | 20×16 cells × 3 B `{glyph, fg, bg}` — the HUD/dialog text plane |
| OAM | `0x8BC0` | 384 B (0x180) | `0x8D40` | 64 sprites × 6 B `{pat, x:i16, y:i16, attr}` |
| PAL | `0x8D40` | 512 B (0x200) | `0x8F40` | 256 × RGB565 |
| REGS | `0x8F40` | 16 B (padded) | `0x8F50` | `scroll_x:u16, scroll_y:u16, ctrl:u16, oam_count:u8` (+ padding) |
| FB | `0x8F50` | 20,480 B (0x5000) | `0xDF50` | 160×128 indexed framebuffer — composed here, converted through PAL on `PRESENT` |

Region base constants (`PPU_PAT`, `PPU_FONT`, `PPU_TILEMAP`, `PPU_PAL`) are defined in `lib/ppu.lib`
and **must** match this table exactly — there's no runtime negotiation, they're baked into both the
C++ core and the compiled library. (`PPU_PAL` sits at `0x8D40` rather than the more "expected"
`0x8C00` because TEXTMAP grew from 2 to 3 bytes/cell partway through development — a reminder that
these offsets are load-bearing, not incidental, if you ever touch `ppu.cpp`'s layout.)

Command stream, DMA/upload paths, and the "why a separate chip-shaped RAM" rationale are covered in
[Architecture → PPU](architecture.md#ppu); the byte-level wire protocol for the command stream is
documented in `lib/ppu.lib`'s header comment.
