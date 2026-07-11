# Architecture

EMU16 is one system built in four layers: **hardware** you can hold, **firmware** that runs on it,
a **shared emulator core** that also runs without the hardware, and **software** (compiler + tools)
that targets it from a dev machine. This doc goes deep on how those layers fit together. For the
30-second version see the [README](../README.md).

## The four layers

```
┌─────────────────────────────────────────────────────────────────────┐
│  SOFTWARE (dev machine)                                             │
│  compiler (src/) · libraries (lib/) · asset pipeline (tools/)       │
│  produces: game.rom + game.pak                                      │
└───────────────────────────────┬───────────────────────────────────--┘
                                 │ game.rom / game.pak
                 ┌───────────────┼────────────────┐
                 ▼               ▼                ▼
        ┌────────────────┐ ┌───────────┐  ┌──────────────────┐
        │ FIRMWARE        │ │ pc_emu.exe│  │ browser sim       │
        │ (ESP32-S3)      │ │ (desktop) │  │ (WASM, no install)│
        │ Arduino shim    │ │ CLI shim  │  │ JS shim           │
        └────────┬────────┘ └─────┬─────┘  └─────────┬─────────┘
                 │                │                  │
                 └────────────────┼──────────────────┘
                                  ▼
                 SHARED CORE — emu.cpp (CPU) + ppu.cpp (PPU)
                 compiled three ways, one ISA, no drift
                                  │
                                  ▼
                        ┌──────────────────┐
                        │ HARDWARE          │
                        │ ESP32-S3 · TFT ·  │
                        │ buttons · flash   │
                        └──────────────────┘
```

Only the top box (**hardware**) is physical. Everything else — firmware, the shared core, and the
two software-only hosts (`pc_emu`, the browser sim) — is the same code wearing three different host
shims. A game compiled once (`game.rom`) runs unmodified on all three.

## Hardware

An ESP32-S3 handheld: a TFT display (driven via `TFT_eSPI`/SPI), 8 buttons (4-direction + A/B/X/Y,
active-low with `INPUT_PULLUP`), and an SD card reader wired but not yet used (LittleFS on internal
flash stands in for now — see [Firmware](firmware.md)). Pin assignments:
[`firmware/src/hw_pins.h`](../firmware/src/hw_pins.h).

## Firmware

A PlatformIO/Arduino project (`firmware/`) that boots the shared core on the ESP32-S3. It does not
contain a second copy of the emulator — `firmware/src/emu_shim.cpp` and `ppu_shim.cpp` `#include`
the real `emulator/emu.cpp` / `ppu.cpp` directly, so firmware and the desktop/browser hosts compile
from **one source file each**, not a fork. `main.cpp` is the Arduino `setup()`/`loop()`: mounts
LittleFS, lists `.rom` files, boots `/menu.rom`, reads buttons, runs one frame of CPU emulation, and
pushes the result to the display (skipping the push when the frame is unchanged from the last one —
a real win since a `pushImage` SPI transfer is the most expensive part of a frame). Full detail:
[docs/firmware.md](firmware.md).

## The shared core

`emulator/emu.cpp` (the CPU) and `emulator/ppu.cpp` (the PPU) are the actual emulator. They compile
three ways with **zero source drift**:

| Build | Entry point | Used by |
|---|---|---|
| Native C++ (static) | `emulator/pc_emulator_main.cpp` | `pc_emu.exe` — desktop dev/test harness |
| Native C++ (Arduino) | `firmware/src/emu_shim.cpp` (includes `emu.cpp`) | the ESP32-S3 handheld |
| Emscripten/WASM | `emulator/emu_wasm.cpp` | the browser simulator |

Each entry point supplies its own thin **host shim**: a syscall handler (ROM listing, save/load,
asset streaming — see [Syscalls](syscalls.md)), a way to load a `.rom` into `memory[0]`, and a way
to display the finished frame. None of that logic lives in `emu.cpp`/`ppu.cpp` themselves — the core
only executes instructions and composes graphics; every "what does the outside world look like"
decision (flash vs. dropped file vs. CLI arg, TFT vs. canvas vs. a `.ppm` file) is host-specific.
This is *why* the same ROM runs identically everywhere: the thing that could drift (the ISA, the PPU
compositor) is compiled once per target from identical source, and the thing that legitimately
differs per target (storage, display) is walled off behind a small, explicit seam.

### CPU

A custom 16-bit CPU: 8 general-purpose registers (`R0`-`R7`; by convention `R6` = frame pointer,
`R7` = stack pointer), a 64 KB little-endian byte-addressed memory space, and flags (zero/sign/carry
in bits 0-2). See [docs/memory-map.md](memory-map.md) for the full address space layout and calling
convention.

Instructions are fixed-width 16-bit words with three encodings (`src/backend/emu_isa.py`):

- **R-type** — `opcode:5 reg1:3 reg2:3 size:1 lower:1 (reserved:1) unused:2` — register/register ALU
  ops (`ADD`, `SUB`, `AND`, `CMP`, ...), `MOV`.
- **M-type** — `opcode:5 reg1:3 base_reg:3 size:0 lower:1 mem:1 abs:1` + a 16-bit trailing
  displacement/address — memory ops (`LDR`/`STR`/`LDROFF`/`STROFF`), either **base+offset** or, with
  the `abs` bit set (`pack_m_type_abs`), an **absolute address** (base register ignored). This one
  spare bit is how absolute addressing was added without a new opcode — see the opcode-space note
  below.
- **I-type** — `opcode:5 reg1:3 ...` + a trailing 8- or 16-bit immediate — `LDI`, jumps, `CAL`.

The opcode field is 5 bits (32 slots) and **all 32 are in use** — the ISA has no room for a new top-
level instruction. The precedent above (`ABS_FLAG`, an unused header bit repurposed into a new
addressing mode on an *existing* opcode) is the intended way to extend it further: add flag-bit
modes to instructions that still have spare header bits, rather than trying to widen the opcode
field itself.

Opcodes: `NOP HLT LDI_LO LDI_HI LDI STRI STR LDR MOV ADD SUB AND OR XOR SHL SHR CMP JZ JNZ JS JNS JC
JNC JMP PSH POP CAL RET LDROFF STROFF MUL DIV` (`src/backend/emu_isa.py`).

### PPU

A NES-2C02-inspired tile/sprite/text compositor (`emulator/ppu.cpp` / `ppu.h`), modeled as a
**separate unit** from the CPU with its own ~56 KB graphics RAM (see
[docs/memory-map.md](memory-map.md#ppu-graphics-ram) for the region layout). The CPU never reads or
writes that RAM directly — it builds a small **command stream** each frame (`lib/ppu.lib`) and hands
it to the PPU as a byte buffer:

```
0x01 SET_REG [reg:1][val:2]              0x04 TEXT    [x,y,w,h][w*h x {glyph,fg,bg}]
0x02 OAM     [count:1][count x sprite]   0x05 PAL     [first:1][count:1][count x rgb565:2]
0x03 TM_EDIT [x,y,w,h][w*h tiles]        0x06 PRESENT []
```

`ppu_receive()` copies that buffer in and executes it; bulk art (tilesets, sprite sheets, palettes)
skips the command stream entirely and goes straight into PPU RAM via `ppu_write()` (backing
`sys_ppu_dma`/`sys_ppu_upload`). This design has one invariant that everything else follows from:
**the PPU never touches CPU memory (`cpu_instance.memory`)**. On today's single-chip build,
"crossing the bus" is just a function call and a `memcpy`; the wire format was designed so that a
future two-chip split (the PPU as a second physical MCU talked to over SPI) is the *same bytes*
crossing a real wire instead of a function call — no game code would need to change.

Per frame the PPU composes in fixed order — **background tilemap → sprites → text plane** — into an
indexed framebuffer, then converts that through its 256-entry RGB565 palette on `PRESENT`. Collision
is deliberately **not** part of the PPU: the CPU keeps its own resident tile grid + a `tile_flags`
property table and queries it directly (`map_solid_at`, in `lib/map.lib`) — the PPU's tilemap is a
*rendering* structure, the CPU's grid is the *simulation* structure, and they're allowed to describe
the same world differently (e.g. one tile id per PPU slot vs. a coarser solid/not-solid bit).

## Software (the dev-machine side)

### Compiler (`src/`)

Hand-written, no third-party parser/codegen libraries. Pipeline (`src/compiler/compiler.py`):

```
game.txt + includes ─┬─▶ Tokenizer (regex-NFA)  ─▶ Parser (recursive descent) ─▶ AST
                      │                                                          │
                      └─ include splicing (no linker: each file parsed once, ─┘
                         merged into one ProgramNode; duplicate top-level names
                         across included files are a hard "Link Error")
                                                                                  ▼
                                                          class_expander (monomorphizes `class`
                                                          into plain globals + mangled functions)
                                                                                  ▼
                                                          SemanticAnalyzer (types, scopes,
                                                          address-taken analysis for codegen)
                                                                                  ▼
                                                          TACGenerator (AST -> three-address IR)
                                                                                  ▼
                                                          Optimizer (const-fold/-prop, dead-store
                                                          elimination, copy-chain collapsing, ...)
                                                                                  ▼
                                                          EmuBackend (linear-scan register
                                                          allocator -> ISA instruction emission)
                                                                                  ▼
                                                                             game.rom
```

`include "path";` is handled before tokenizing (a regex strip + recursive file walk) and is a pure
splice, not a linker step — there's no relocation or symbol resolution across translation units, so
two included files that both define a function with the same name is a compile-time `Link Error`,
not a silent last-one-wins. A small "hardware constant prelude" (`SCREEN_WIDTH`, `SCREEN_HEIGHT`,
`VRAM_START`, `PRAM`, `INPUT_ADDR`) is injected as `const`s ahead of every program so they match
`emulator/definitions.h` by construction.

`main.py` → `src/compiler/compiler.py:main()` is the CLI (`python main.py game.txt --save-rom
name`); useful debug flags: `--print-tokens/-ast/-tac/-optimized-tac/-alloc/-emit`, `--no-optimize`.

A vestigial `src/backend/X86Backend.py` (emits x86 assembly + a `runtime.asm`) also exists from an
early prototyping phase, before the custom 16-bit CPU direction was chosen. It is not wired into the
CLI (`compiler.py` only ever instantiates `EmuBackend`) and produces nothing any current host runs.

This is the big-picture shape only. For the language itself (grammar, types, every syntax quirk),
see [docs/language.md](language.md). For what happens *inside* each pipeline stage — the TAC IR's
`Var`-identity rules, the optimizer's actual passes, the register allocator, and the backend's
operand-aware code emission (byte/word handling, ISA addressing-mode quirks, known limitations) —
see [docs/compiler.md](compiler.md).

### Libraries (`lib/`)

`.lib` files are plain EMU16 source, `include`d like any file — there's no special library format.
See [docs/libraries.md](libraries.md) for the full per-function reference.

### Asset pipeline (`tools/`)

Python, stdlib-only (a hand-written indexed-PNG reader in `tools/png.py` — no Pillow dependency).
Turns hand-drawn art and hand-painted tilemaps into the `.pak` a ROM streams from at runtime. See
[docs/tools.md](tools.md) and [docs/file-formats.md](file-formats.md).

### The two software-only hosts

- **`pc_emu.exe`** — a native, one-shot CLI harness: load a ROM, run N frames, dump the framebuffer
  to a `.ppm` and print a register/instruction/checksum summary. No display, no interactivity — built
  for scripted regression testing. [docs/pc-emulator.md](pc-emulator.md).
- **The browser simulator** (`simulator/`) — an interactive, no-install dev target: drop a `.rom` (+
  its `.pak`) into a page, watch it run, with an FPS cap, a CPU-instruction-budget slider, and live
  CPU/PPU memory viewers. [docs/simulator.md](simulator.md).

Both wrap the exact same WASM/native build of `emu.cpp`+`ppu.cpp` as the firmware — they exist so you
can develop and regression-test a game without touching real hardware, not because they run a
different emulator.
