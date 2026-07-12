# Language reference

This is the authoritative EMU16 language reference. It replaces pointing at `src/parser/Parser.py`'s
header comment — that comment is a leftover EBNF sketch from an early version of the grammar and no
longer matches what the parser actually accepts (no `for`, `switch`, arrays, pointers, structs, or
classes in it, all of which exist and are covered below). Everything here is verified against the
current parser/analyzer source, not against that comment.

## Program structure

A whole source file (and every `include`d library) is one top-level block:

```c
{
    // declarations only: var, const, struct, class, function defs
}
```

Only these may appear at the top level: `var` (global variables/arrays/struct instances), `const`,
`struct` defs, `class` defs, `new ClassName inst;` instantiations, and function definitions. No
top-level statements, no nested function definitions (a function inside a function is a semantic
error), no code outside a function body. Exactly one function named `main`, returning `int`, taking
no parameters, is required — the compiler rejects a program without it.

`include "path";` splices another file's whole top-level block into this one (recursively, each
file parsed once) *before* semantic analysis — it is not a linker step. Two included files defining
the same top-level name is a hard compile-time error ("Link Error: duplicate symbol"), not a
last-one-wins.

## Types

| Type | Size (scalar) | Size (array element / struct field) | Notes |
|---|---|---|---|
| `int` | 2 bytes | 2 bytes | 16-bit, values roughly -32768..65535 (see [Numeric mixing](#numeric-mixing-and-truncation)) |
| `byte` | **2 bytes** | **1 byte** | see below — a scalar `byte` is *not* 1 byte |
| `bool` | 2 bytes | — | `true`/`false`; not interchangeable with int/byte in `&&`/`||`/`if`/`while` conditions |
| `void` | — | — | function return type only; illegal for a variable, array, parameter, or struct field |

**The scalar-`byte`-is-word-sized quirk:** a `byte` variable only actually occupies **one packed
byte** inside a `byte[]` array or a struct field. A plain scalar `var byte x;` still reserves a full
2-byte data-section slot and is treated as numerically identical to `int` (the semantic analyzer's
`_is_numeric` explicitly treats `int`/`byte` as one numeric family, freely assignable to each other,
truncating silently on narrowing). `byte` only changes storage *width* where width is actually
tracked: array elements (`elem_size = 1`) and struct fields (packed with no padding, `offset += 1`).
If you need real 1-byte-per-element packing, use `byte[]` or a struct field — not a bare scalar.

## Literals

- Decimal integers (`42`) and hex (`0xFF`, `0xB000`) — both accepted anywhere a number is expected,
  including array sizes and `const` values.
- A leading `-` on a numeric literal (`-5`) is lexed as one token (`SIGNED_NUMBER`); `-x` for a
  non-literal expression is unary minus (see [Operators](#operators-and-precedence)), a different
  AST node (`NegNode`, lowered to `0 - x` in codegen — there's no dedicated negate instruction).
- `true` / `false` — boolean literals, type `bool`.
- String literals (`"hi\n"`) are hand-lexed by the tokenizer (not the general token patterns), with
  escapes `\n \t \r \0 \\ \"`. A string literal is **hoisted** to an anonymous, NUL-terminated
  `byte` array (`__str0`, `__str1`, ...) declared at the top of the compilation unit; the literal
  itself evaluates to that array's address (`&__strN`), reusing ordinary array-literal baking and
  address-of codegen — there's no separate "string" runtime type.
- No floating point. No character literals (`'a'` is not valid syntax — write the ASCII code).

## Operators and precedence

```
+  -  *  /  %        arithmetic (int only; % is truncated: a - (a/b)*b, no MOD instruction)
&  |  ^  ~           bitwise (binary AND/OR/XOR; ~ is unary NOT, lowered to x ^ 0xFFFF)
<<  >>               shift
<  >  ==  <=  >=  != comparison (each yields bool)
&&  ||               logical (bool operands only — see the parenthesization rule below)
&  *                 address-of / pointer-dereference (unary; the SAME & token as bitwise-AND,
                     disambiguated by parser position — see Pointers)
```

**Precedence, loosest to tightest: comparison < bitwise < shift < additive (`+ -`) < term
(`* / %`).** This is a real, verified difference from C, not just a stylistic note:

```c
if x & 1 == 0 { ... }        // parses as (x & 1) == 0 — verified via --print-ast
```

In C, `==` binds *tighter* than `&`, so `x & 1 == 0` parses as `x & (1 == 0)` — the classic C
gotcha that forces everyone to write `(x & 1) == 0` out of habit. In EMU16 the grammar is the other
way around: the entire bitwise/shift/arithmetic chain is parsed as one self-contained `<expression>`
*before* the parser even looks for a comparison operator, so **comparisons always wrap around
bitwise expressions, never the reverse.** The parens in `if (x & 1) == 0` (as used throughout
`examples/` and `lib/`) are there for human readability, matching C convention — the grammar does
not require them.

**`&&`/`||` require each side to be independently parenthesized** — this one *is* required, not
just stylistic. The grammar only recognizes a logical operator in the form:

```c
if (a > 0) && (b > 0) { ... }     // required: (cond) && (cond)
if a > 0 && b > 0 { ... }          // syntax error — no bare-condition && path exists
```

There is no path in the parser that accepts a logical operator between two *unparenthesized*
conditions — check `(cond)` yourself before combining. Chains of 3+ are fine as long as each operand
has its own parens: `(a) && (b) && (c)`.

**No implicit truthiness.** An `if`/`while`/`for`-condition must have type `bool` — a bare
`if x { ... }` where `x` is an `int` is a **type error**, not "nonzero is true" like C. Write an
explicit comparison (`if x != 0 { ... }`) or use an actual `bool` variable.

**There is no `!` (logical NOT).** Negate a boolean by comparing (`if b == false { ... }`) or via
bitwise (`b ^ 1`, since bools are 0/1 words).

## Numeric mixing and truncation

`int` and `byte` are mutually assignable in either direction (`_assignable`: same-type, or both
numeric) with **silent truncation** on narrowing — no separate cast syntax exists or is needed.
`bool` is its own type and is **not** numeric-assignable to/from `int`/`byte` (you cannot do
`var int x = someBool;`).

## Control flow

- `if <condition> { }` / `else if <condition> { }` / `else { }` — standard chain, condition must be
  `bool` (parens around the whole condition are optional; see precedence above).
- `while <condition> { }`.
- `for ( <init> ; <condition> ; <post> ) { }` — `init` is an optional `var` declaration or a plain
  assignment (no comma-separated multi-clause init); `post` is likewise a single assignment; either
  may be omitted (`for (;cond;) {}` is legal) but the two `;` are always required.
- `break;` / `continue;` — only legal inside a `while`/`for` body (checked via a loop-depth counter,
  not lexical nesting rules); `continue` in a `for` jumps to the **post** clause, not straight back
  to the condition.
- `switch (expr) { case C: ... case C2: ... default: ... }` — see below.

### `switch`

```c
switch (level) {
    case 0: return handle_intro();
    case 3: return handle_boss();
    default: return handle_normal();
}
```

- `expr` must be `int` or `byte`.
- Every `case` value must be a **compile-time constant** (folded by the same const-evaluator that
  resolves `const NAME = ...;` — literals, named consts, and constant arithmetic over them); a
  non-constant case value is a compile error.
- **Auto-break, no fallthrough** — each case body implicitly jumps to the switch's end; there is no
  way to fall through to the next case.
- Duplicate case values are a compile-time error.
- Compiles to an **O(1) indirect-jump table**, not an if-else chain — cost is constant regardless of
  how many cases exist (see [compiler.md](compiler.md#switch-lowering-a-worked-example) for exactly
  how).

## Functions

```c
int add(int a, int b) { return a + b; }
void nothing() { }
```

- Parameters: up to 3 arrive in registers (`R1`-`R3`); a 4th and beyond are passed on the stack by
  the caller (see [memory-map.md](memory-map.md#calling-convention)). No default arguments, no
  varargs, no overloading (one global name per function).
- `void`-returning functions may omit `return;`; a compiler-inserted `ret` is added if the body
  doesn't already end in one. Non-`void` functions must return a value on every path the analyzer
  can see (not exhaustively verified — a non-`void` function that falls off the end without
  `return` is not statically rejected, it's just undefined what ends up in `R0`).
- **Naked functions**: a function whose *entire body* is a single `asm { }` block skips the
  compiler-generated prologue/epilogue entirely — arguments arrive in `R1`-`R3` as raw register
  values, and the `asm` block must supply its own `RET`. Every syscall/PPU wrapper in `lib/` is
  written this way. A function with any other statement (even alongside an `asm` block) gets the
  normal prologue/epilogue.
- No nested functions, no closures, no function pointers as a *language* feature — though the
  `switch` jump table and the underlying `JMP`/`CAL` register-mode ISA support are exactly the
  building blocks a future function-pointer feature would reuse (see
  [compiler.md](compiler.md#switch-lowering-a-worked-example)).

## Arrays

```c
var int scores[10];                     // zero-initialized
var byte pal[3] = {1, 2, 4};             // literal initializer, baked into the ROM image
const N = 64;
var int buf[N];                          // const-named size
```

- `var <type> name[N];` or `var <type> name[N] = { c0, c1, ... };`. `N` is a literal or a `const`
  name (resolved to a literal before codegen); it must be positive.
- An initializer may supply fewer than `N` elements (the rest are zero); it cannot supply more.
  Every initializer element must itself be a compile-time constant expression (literals, consts,
  and constant arithmetic — **not** a runtime expression, and **not** a function call).
- **Array-literal initializers are baked directly into the ROM image** at the array's data address
  (not written by runtime code) — they're present in memory the instant the ROM loads, on every
  host, with zero startup cost. (This is also how the built-in 8×8 font ships — see
  `lib/ppu.lib`'s `font8x8`.)
- Arrays always live in the data section regardless of the declaring scope (a "local" array is
  still a fixed global address under the hood — its *identity*, i.e. which storage slot reads and
  writes agree on, still depends on matching scope metadata exactly; see
  [compiler.md](compiler.md#var-identity-the-name-storage-scope-type-tuple)).
- Indexing: `arr[i]` (read) / `arr[i] = v;` (write). The index may be any numeric expression.
  `int` arrays scale the index by 2 (word stride); `byte` arrays don't scale it at all.

## Pointers

```c
var int x = 5;
var int p = &x;      // address-of: p now holds x's address
var int y = *p;       // dereference (rvalue)
*p = 10;               // dereference (lvalue) -- x is now 10
```

- `&name` — address-of a named variable (not an array element or struct field expression directly;
  take the address of the whole array/struct and do arithmetic instead). Taking a variable's address
  **forces the compiler to give it a stable stack slot** instead of a register for the rest of the
  function (see [compiler.md](compiler.md#the-register-allocator) — this is an allocator
  correctness requirement, not a performance choice).
- `*expr` as an rvalue dereferences; `*expr = value;` as a statement stores through the pointer.
- Pointers are just `int`s (16-bit addresses) — no pointer type distinct from `int`, no pointer
  arithmetic type-scaling (`p + 1` advances by 1 raw address unit, not by `sizeof` anything — do the
  multiplication yourself, exactly like the array-indexing codegen does internally).
- Dereferencing has no compiler-tracked width: `*p` defaults to a 2-byte (word) load/store. There is
  no `byte`-width deref syntax at the language level — code that needs a byte-wide pointer access
  goes through an explicit helper (e.g. `io.lib`'s `peek_byte`/`poke_byte`, which are word-in/
  byte-in-memory naked-asm functions, not raw `*p` on a byte pointer).

## Structs

```c
struct Enemy { int hp; byte kind; }
var Enemy e;
var Enemy swarm[20];
e.hp = 10;
swarm[3].kind = 1;
```

- `struct Name { <type> <field>; ... }` — fields must be `int`, `byte`, or `bool` (**no nested
  structs, no array fields** — v1 scope only).
  Byte-packed, no padding: `int`/`bool` fields cost 2 bytes, `byte` fields cost 1, offsets assigned
  in declaration order.
- `var Struct name;` (one instance) or `var Struct name[N];` (an array of instances, `N` a literal
  or const) — data-only, no methods.
  Access via `.`: `name.field` / `name.field = value;`, or for an array, `name[i].field` /
  `name[i].field = value;`.
- There is no struct-pointer type and no passing a struct by value/reference to a function — the
  established pattern for "many entities" is a global struct array + passing an index, not a
  pointer to one element (see any of `arena.txt`'s object tables).

## Classes

```c
class Enemy {
    var int hp = 10;
    var Sprite anim;              // composition: another class as a field

    void hurt(int dmg) {
        self.hp = self.hp - dmg;
    }
}
new Enemy slime;
slime.hurt(3);
```

Classes are **compile-time-only sugar**: `new Class inst;` clones the class's fields and methods
into plain globals and free functions with an `inst__`-prefixed mangled name (`slime__hp`,
`slime__hurt`), and every `inst.field` / `inst.method()` / `self.field` call site is rewritten to
the mangled name — all *before* semantic analysis runs, so the rest of the compiler (type checking,
TAC generation, codegen) never knows classes exist. There is zero runtime overhead and zero backend
code dedicated to classes.

**v1 scope, enforced by the expander itself:**
- A class field must be a **primitive** (`int`/`byte`/`bool`) or a **single composed class
  instance** (`var Other sub;` → `self.sub.method()` works, mangled to `inst__sub__method`).
  Arrays and structs as class fields are **not supported** — put big buffers in an ordinary global
  instead of inside a class.
- **No inheritance.**
- **No runtime-indexed object arrays** (`new Enemy foes[10]` is not a thing) — every instance is a
  statically-named, compile-time-known object. For a swarm of many identical entities, use a
  **struct array** instead (see [Structs](#structs)) — classes are for a handful of named,
  semantically distinct objects (the player, a scene's Animator, ...), not for populations.
- `self` is only valid inside a method body (referencing it elsewhere is a compile-time error).
- A method call one level deep (`obj.method()`) or a chained field access two-plus levels deep
  (`obj.sub.field`, `self.sub.method()`) both work; the mangling is literally `__`-joined path
  segments.

## Inline assembly

```c
int sys_time() {
    asm { LDI R0, 6
          LDI R3, 0xFFFE
          STR R0, [R3], byte
          RET }
}
```

- `asm { ... }` is captured as one raw token by the tokenizer itself (brace-depth-counted, so it can
  contain `[`, hex literals, and anything else that isn't otherwise tokenizable) — it is *not*
  parsed by the normal expression grammar at all.
- A tiny built-in mini-assembler (`EmuBackend._emit_asm_line`) accepts: `NOP HLT RET LDI STRI MOV
  PSH POP LDR STR JMP JZ JNZ JS JNS JC JNC CAL` + the ALU ops (`ADD SUB AND OR XOR SHL SHR MUL DIV
  CMP`) + `DW`/`DB` (raw word/byte literals) + `label:` definitions. Registers are `R0`-`R7`;
  `[Rn]` denotes a memory operand, with an optional trailing `byte` for a 1-byte-wide `LDR`/`STR`.
  Labels defined inside one `asm` block are automatically made unique per block (so `loop:` in two
  different functions' asm bodies never collides) — a bare name not defined in the same block (e.g.
  a real function called via `CAL`) is left as-is and resolved as a normal fixup.
- Calling convention inside `asm` is entirely your responsibility: for a naked function, arguments
  are already in `R1`-`R3` on entry and the block must end in `RET`; for `asm` used as one statement
  inside an otherwise-normal function, the compiler's own prologue/epilogue still wraps it.

## Comments

Only `// line comments` — no `/* block comments */`. A comment inside an `asm { }` block is stripped
by the mini-assembler at emission time (each line's `;` or `//` suffix is trimmed), not by the main
tokenizer.

## A dead keyword: `print`

`print(expr);` still **parses** and still **type-checks** (`PrintNode` exists in the grammar and the
semantic analyzer happily assigns it a type) — but the backend hits it and raises
`RuntimeError("Print is not supported in EmuBackend")` the moment it tries to generate code for one.
It's a leftover from an earlier VRAM-console-output era of the language; nothing in `examples/` or
`lib/` uses it, and nothing should. `print` is still a reserved keyword (you cannot name a variable
`print`), it just has no working code generator. If you see it in an old file, that file predates
the current backend and needs a real host output call (`tile_text`/`tile_number` on the PPU) instead.

## Compile-time constants

```c
const SCREEN_TILES_W = 20;
const HALF = SCREEN_TILES_W / 2;   // consts may reference earlier consts + constant arithmetic
```

`const NAME = <expr>;` folds to a plain integer at compile time (phase 0 of semantic analysis, before
anything else runs) and is then stripped — codegen never sees a `ConstDefNode` at all; every
reference to the name becomes a literal `Const` operand. Consts must be declared before use
(declaration order, across the whole merged translation unit including included libraries) and may
be used as array sizes. The supported constant-expression operators are `+ - * / % & | ^ << >>` and
unary `-`/`~` over literals and other consts — nothing involving a variable or a function call.
