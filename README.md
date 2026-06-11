# Toy Compiler

A compiler for a small C-like language that targets a custom 16-bit CPU ISA, built for a
Game Boy-style handheld console running on an ESP32. Every stage of the pipeline — lexer, parser,
semantic analysis, IR generation, optimizer, register allocator, and code generator — is written by
hand, without external compiler libraries.

The output is a flat `.rom` image loaded directly by the ESP32 firmware (or the included PC
desktop emulator for development and testing).

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

```c
if (a > b) do {
    // ...
}

while (count < 10) do {
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
        while (x < SCREEN_WIDTH) do {
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

### Desktop Emulator (PC test runner)

`pc_emu.exe` is a statically linked desktop build of the same emulator core used on the ESP32.
It runs a ROM for a fixed number of frames and writes the final framebuffer as a PPM image.

```bash
# Run for 1 frame, print result
pc_emu.exe --rom build/roms/demo.rom --frames 1

# Output:
# REGS R0=0x002A R1=... ...
# RESULT halted=1 frames=1 return=42 pc=0x1032 ... frame=build/pc_emulator/frame.ppm
```

Build the PC emulator (MinGW / GCC):

```bash
g++ -static pc_emulator_main.cpp emu.cpp -o pc_emu.exe -std=c++17 -O2
```

### Regression Tests

```bash
python run_tests.py
```

Runs all 15 tests (M1 + M2), compiles each source file, executes it in the desktop emulator, and
checks the `return=` value:

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
├── lib/
│   └── io.lib          IO library (plot, fill, set_palette, poke_byte, peek_byte, …)
├── examples/           Sample programs + regression test sources
├── emu.cpp             16-bit CPU emulator core (shared with ESP32 firmware)
├── emu.h
├── definitions.h       Hardware addresses, screen dimensions
├── pc_emulator_main.cpp  PC test harness (builds pc_emu.exe)
├── run_tests.py        Regression test runner
└── main.py             Compiler entry point
```

`emu.cpp` and `pc_emulator_main.cpp` live here because every ISA change requires coordinated
updates to both the emulator and `emu_isa.py`. Having them in one repo makes that reflex automatic
and keeps the test suite self-contained.

---

## Requirements

- Python 3.7+
- GCC / MinGW (to rebuild `pc_emu.exe` after ISA changes)
