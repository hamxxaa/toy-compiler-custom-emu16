# EMU16

A homebrew game platform built from scratch: a custom 16-bit CPU ISA, a C-like compiler that
targets it, an ESP32-S3 handheld console, and a browser simulator — all in one repo.

| Layer | What it is |
|---|---|
| **Language** | C-like (types, arrays, pointers, bitwise, else/else-if, inline asm) |
| **Compiler** | Hand-written: Thompson NFA lexer → recursive descent parser → TAC IR → linear-scan allocator → `.rom` |
| **CPU / ISA** | Custom 16-bit, 8 registers, 64 KB address space |
| **Targets** | ESP32-S3 handheld · PC desktop emulator · browser WebAssembly simulator |

Write a program once, run it on real hardware, in the terminal, or in a browser tab.

```bash
# Compile
python main.py examples/demo.txt --save-rom demo

# Run on PC
pc_emu.exe --rom build/roms/demo.rom --frames 1

# Flash to handheld (PlatformIO)
make flash && make uploadfs
```

---

## Architecture

### CPU Summary

| Property | Value |
|---|---|
| Word size | 16-bit |
| Address space | 64 KB (byte-addressable, little-endian) |
| Registers | R0–R7 (R6 = FP, R7 = SP) |
| Calling convention | R1–R3 = args, R0 = return value, R4–R5 = callee-saved |

### Memory Map

| Region | Address Range | Size | Purpose |
|---|---|---|---|
| Data / globals | `0x0008` – `0x0FFF` | ~4 KB | Global variables, arrays, string data |
| Code | `0x1000` – `0xADFD` | ~39 KB | Compiled program |
| INPUT | `0xADFF` | 1 byte | Button state (read-only) |
| PRAM | `0xAE00` – `0xAFFF` | 512 B | 256-entry RGB565 palette |
| VRAM | `0xB000` – `0xFFFF` | 20 KB | 160×128 indexed framebuffer |

### Compiler Pipeline

```
Source (.txt)
  └── Tokenizer          hand-written regex engine (Thompson's NFA)
        └── Parser           recursive descent → AST
              └── SemanticAnalyzer   type checking, scope, address-taken tracking
                    └── TACGenerator       three-address code (IR)
                          └── Optimizer          constant folding + propagation
                                └── EmuBackend         liveness analysis, linear-scan register allocator → .rom
```

---

## Language Reference

### Types

| Type | Size in register | Size in array | Notes |
|---|---|---|---|
| `int` | 16-bit | 2 bytes | default integer |
| `byte` | 16-bit (zero-extended) | 1 byte | truncated on array store |
| `bool` | 16-bit | 2 bytes | `true` / `false` literals |
| `void` | — | — | function return only |

### Variables

```c
var int  a;               // declaration (zero-initialized)
var int  b = 10;          // initialization
var byte c = 0xFF;        // hex literal (byte or int)
var int  d = 0xB000;      // hex address constant
```

### Operators

| Category | Operators | Operand types | Result |
|---|---|---|---|
| Arithmetic | `+` `-` `*` `/` | int/byte | int |
| Bitwise | `&` `\|` `^` `~` (unary) | int/byte | int |
| Shift | `<<` `>>` (logical) | int/byte | int |
| Comparison | `<` `>` `==` `<=` `>=` `!=` | int/byte | bool |
| Logical | `&&` `\|\|` | bool | bool |

Precedence, loosest to tightest: `& \| ^`  <  `<< >>`  <  `+ -`  <  `* /`. The bitwise
operators share one flat level (left-to-right), so `a \| b & c` is `(a \| b) & c` — use
parentheses when mixing them. Comparisons bind looser than everything, so `x & 1 == 1`
parses as `(x & 1) == 1` (unlike C).

```c
var int masked  = color & 0x0F;     // bitwise AND
var int merged  = hi << 8 | lo;     // shift then OR
var int flipped = ~bits;            // bitwise NOT (16-bit)
var int parity  = v ^ key;          // XOR

if (x > 0) && (x < SCREEN_WIDTH) {   // logical AND on bools
    plot(x, y, 1);
}
```

Shift amounts are masked to 4 bits by the CPU (max shift 15); `>>` is a logical shift
(zero-fill). `~x` compiles to `x ^ 0xFFFF` — there is no NOT opcode.

### Arrays

Arrays are always in the data section (static storage). All sizes must be compile-time constants.

```c
var int  arr[8];          // 8-element int array  (16 bytes)
var byte buf[256];        // 256-element byte array (256 bytes)

arr[2] = 42;              // write
var int x = arr[2];       // read
```

Int element offset uses a shift (`i << 1`) rather than multiply — keeps access cheap.

### Pointers

```c
var int  val = 99;
var int  p   = &val;      // address-of: p holds the address of val
var int  q   = *p;        // deref read:  q = mem[p]
*p = 77;                  // deref write: mem[p] = 77
```

`&x` forces `x` onto the stack frame so it has a stable address. Global arrays also expose their
base address through `&arr[0]` or by passing the array name to an asm function.

### Control Flow

Parentheses around conditions are optional — the opening `{` delimits the condition.

```c
if a > b {
    // ...
} else if a == b {
    // ...
} else {
    // ...
}

while count < 10 {
    count = count + 1;
}
```

### Functions

```c
int add(int a, int b) {
    return a + b;
}
```

### Inline Assembly

The `asm { }` block emits raw instructions verbatim. The programmer is responsible for register
state. Labels inside the block are local to that block.

```c
void delay() { asm {
    LDI R0, 1000
.loop:
    SUB R0, 1
    JNZ .loop
    RET
} }
```

A function whose entire body is a single `asm { }` block is **naked**: no prologue or epilogue is
emitted. ABI for naked functions: args arrive in R1/R2/R3, return value goes in R0, `RET` is the
programmer's responsibility.

Supported mnemonics in inline asm:

```
LDI  MOV  ADD  SUB  MUL  DIV  AND  OR
CMP  JMP  JZ   JNZ  PSH  POP  CAL  RET
LDR  STR  STRI HLT  SHL  SHR
```

Memory operands: `[Rn]` (word), `[Rn], byte` (byte width). Hex immediates (`0xB000`) and decimal
immediates are both accepted.

### Include

Merge a library file into the current program before compilation. There is no separate link step —
the library source is parsed and spliced into the AST.

```c
include "lib/io.lib";

{
    int main() {
        plot(80, 64, 2);   // call a function defined in io.lib
        return 0;
    }
}
```

### Hardware Constants (injected automatically)

These names are always available without an include:

| Name | Value | Description |
|---|---|---|
| `VRAM_START` | `0xB000` | Base address of the framebuffer |
| `INPUT` | `0xADFF` | Button state register |
| `PRAM` | `0xAE00` | Palette RAM base |
| `SCREEN_WIDTH` | `160` | Display width in pixels |
| `SCREEN_HEIGHT` | `128` | Display height in pixels |

---

## IO Library (`lib/io.lib`)

The IO library is written in the language itself using naked asm functions. Include it to get:

| Function | Signature | Description |
|---|---|---|
| `plot` | `void plot(int x, int y, int color)` | Write a color index to VRAM |
| `fill` | `void fill(int color)` | Clear the entire framebuffer |
| `set_palette` | `void set_palette(int index, int color)` | Write one RGB565 palette entry |
| `read_input` | `int read_input()` | Read the button state byte |
| `poke_byte` | `void poke_byte(int addr, byte val)` | Write one byte to an arbitrary address |
| `peek_byte` | `byte peek_byte(int addr)` | Read one byte from an arbitrary address |

---

## Example Programs

### Hello, VRAM

```c
include "lib/io.lib";
{
    int main() {
        fill(0);
        var int x = 0;
        while x < SCREEN_WIDTH {
            plot(x, 64, 2);
            x = x + 1;
        }
        return 0;
    }
}
```

### Array Fill and Read

```c
{
    int main() {
        var int scores[4];
        scores[0] = 10;
        scores[1] = 20;
        scores[2] = 30;
        scores[3] = 40;
        return scores[2];   // returns 30
    }
}
```

### Pointer Swap

```c
{
    int main() {
        var int a = 1;
        var int b = 2;
        var int pa = &a;
        var int pb = &b;
        var int tmp = *pa;
        *pa = *pb;
        *pb = tmp;
        return a;   // returns 2
    }
}
```

---

## Usage

```bash
# Compile a source file (produces build/roms/<name>.rom)
python main.py examples/demo.txt --save-rom demo

# Inspect compiler internals
python main.py examples/demo.txt --print-tokens
python main.py examples/demo.txt --print-ast
python main.py examples/demo.txt --print-tac
python main.py examples/demo.txt --print-optimized-tac
```

### Building

A `Makefile` covers all build and test targets (requires GNU make — `mingw32-make` on Windows):

```bash
make           # build pc_emu.exe + WASM simulator
make pc_emu    # desktop emulator only  (needs GCC / MinGW)
make wasm      # WebAssembly only       (needs Emscripten on PATH)
make test      # compile + run all 23 regression tests
make verify    # headless WASM cross-check via Node
make clean     # remove all build artifacts
```

Manual equivalents if you don't have `make`:

```bash
# Desktop emulator
g++ -std=c++17 -O2 -static emulator/pc_emulator_main.cpp emulator/emu.cpp -o pc_emu.exe

# WASM module
bash simulator/build.sh
```

### Desktop Emulator

`pc_emu.exe` is a statically linked desktop build of the same emulator core used on the ESP32.
It runs a ROM for a fixed number of frames and writes the final framebuffer as a PPM image.

```bash
pc_emu.exe --rom build/roms/demo.rom --frames 1
# REGS R0=0x002A R1=... ...
# RESULT halted=1 frames=1 return=42 pc=0x1032 ... frame=build/pc_emulator/frame.ppm
```

### Web Simulator

The browser simulator in `simulator/` runs the **exact same `emu.cpp` core**, compiled to
WebAssembly — there is no second emulator implementation to keep in sync. Drag a `.rom` onto the
page to load it; the canvas renders VRAM through the palette, and the debug panel shows registers,
flags, and a live memory view.

Serve over HTTP (WASM won't load from `file://`) and open the page:

```bash
cd simulator && python -m http.server 8000
# then open http://localhost:8000
```

`emulator/emu_wasm.cpp` is the thin `extern "C"` bridge (`emu_init`, `emu_mem`, `emu_run_frame`,
`emu_reg`, …); `simulator/wasm.js` wraps it. Re-run `make wasm` after any ISA change to `emu.cpp`.

`verify_wasm.js` runs every test ROM through the WASM core under Node and checks `R0` — a headless
proof the browser core matches `pc_emu.exe`:

```bash
node verify_wasm.js   # or: make verify
```

### Regression Tests

```bash
python run_tests.py   # or: make test
```

Runs all 23 tests (M1 + M2 + M3 + M4), compiles each source file, executes it in the desktop
emulator, and checks the `return=` value:

```
Test                    expect       got
--------------------------------------------------
test_mul                    42        42  PASS
test_div                     6         6  PASS
test_byte                  300       300  PASS
test_asm                    42        42  PASS
test_asm_loop               15        15  PASS
test_include                42        42  PASS
test_plot                   99        99  PASS
test_fill                    7         7  PASS
test_palette              1234      1234  PASS
demo                        42        42  PASS
test_hex                   255       255  PASS
test_array_int              42        42  PASS
test_array_byte              7         7  PASS
test_pointer                99        99  PASS
test_deref_write            77        77  PASS
test_bitand                 11        11  PASS
test_bitor                 255       255  PASS
test_bitxor                240       240  PASS
test_bitnot                255       255  PASS
test_shift                  36        36  PASS
test_logical                 3         3  PASS
test_else                    2         2  PASS
test_elseif                 30        30  PASS
--------------------------------------------------
ALL PASS: True
```

---

## Project Structure

```
toy-compiler/
├── src/
│   ├── lexer/          Tokenizer + hand-written regex engine
│   ├── parser/         Recursive descent parser, AST node classes
│   ├── analyzer/       Semantic analyzer (type checking, scoping, address-taken)
│   ├── codegen/        TAC generator
│   ├── optimization/   Constant folding + propagation optimizer
│   └── backend/
│       ├── EmuBackend.py       Code generator → .rom
│       ├── emu_isa.py          Opcode constants (mirrors emu.cpp)
│       └── core/
│           ├── allocator.py    Linear-scan register allocator + liveness
│           └── function_frame.py
├── emulator/
│   ├── emu.cpp             16-bit CPU emulator core — single source for all targets
│   ├── emu.h
│   ├── definitions.h       Memory map + screen dimensions (no hardware pins)
│   ├── emu_wasm.cpp        extern "C" bridge for the WASM build
│   ├── pc_emulator_main.cpp  PC test harness (builds pc_emu.exe)
│   └── library.json        PlatformIO descriptor — exposes emu.cpp as a library
├── firmware/               PlatformIO project for the ESP32-S3 handheld
│   ├── platformio.ini      lib_extra_dirs = ../emulator (no emu.cpp copy)
│   ├── src/
│   │   ├── main.cpp        Arduino setup/loop — TFT, LittleFS, input, frame loop
│   │   └── hw_pins.h       ESP32-S3 GPIO pin assignments
│   └── data/               ROM files flashed to LittleFS (pio run -t uploadfs)
├── simulator/
│   ├── wasm.js         EmuCore shim (wraps WASM exports, same API as old cpu.js)
│   ├── app.js          UI logic
│   ├── display.js
│   ├── debug.js
│   ├── index.html
│   └── build.sh        Emscripten build script → emu.js + emu.wasm
├── lib/
│   └── io.lib          IO library (plot, fill, set_palette, poke_byte, peek_byte, …)
├── tests/              Regression test source files
├── examples/           Sample programs
├── Makefile            Build, test, flash, and clean targets
├── verify_wasm.js      Headless WASM test runner (Node)
├── run_tests.py        Regression test runner
└── main.py             Compiler entry point
```

`emu.cpp` is the single source of truth for the CPU core — compiled to a PC binary, to WASM, and
to the ESP32 firmware from the same file. An ISA change only needs to happen once.

---

## Firmware (ESP32-S3)

The `firmware/` directory is a PlatformIO project for the physical handheld. Open the
`firmware/` folder (not the repo root) in VS Code — PlatformIO reads `platformio.ini` from there.
`emulator/emu.cpp` is pulled in via `src/emu_shim.cpp`; there is no second copy of the CPU core.

```bash
# Build + flash the firmware
make flash          # pio run -t upload

# Upload ROM files to LittleFS (do this once after flashing, then only when ROMs change)
make uploadfs       # pio run -t uploadfs
```

To ship a new ROM:
```bash
python main.py mygame.txt --save-rom mygame
copy build\roms\mygame.rom firmware\data\mygame.rom
# edit firmware/src/main.cpp: load_rom_from_flash("/mygame.rom");
make uploadfs
```

### Performance knobs

The main bottleneck is SPI pixel transfer (~12 ms at 27 MHz for a full 160×128 frame).
Current defaults are already tuned: 40 MHz SPI + DMA push (`pushImageDMA`).

| Setting | File | Note |
|---|---|---|
| `SPI_FREQUENCY` | `firmware/platformio.ini` | 40 MHz default; try 80 MHz if your panel supports it |
| `USE_DMA_TO_TFT` | `firmware/platformio.ini` | Enabled — frees CPU during pixel transfer |
| Instruction budget | `emulator/emu.cpp` `run_frame_instructions` | Currently 100 000 per frame; raise if games feel slow |
| `ENABLE_DEBUG_LOGS` | `firmware/src/main.cpp` | Set `true` for Serial output during development |

GPIO pin assignments are in `firmware/src/hw_pins.h`.

---

## Requirements

- Python 3.7+
- GCC / MinGW (to rebuild `pc_emu.exe` after ISA changes)
