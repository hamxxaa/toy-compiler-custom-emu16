# Compiler internals

The pipeline overview in [architecture.md](architecture.md#compiler-src) covers the stage-by-stage
shape (tokenizer → parser → class expander → semantic analyzer → TAC generator → optimizer →
backend). This doc goes deep on the two stages with the most non-obvious behavior: what the TAC IR
actually looks like, and — especially — the backend's register allocator and code emission, where
most of the project's real "gotchas" live. For language syntax/grammar, see
[language.md](language.md); this doc is entirely about *how a valid program becomes bytes*.

## The TAC IR

`src/codegen/TACGenerator.py` lowers the AST to a flat list of `TACInstruction(op, arg1, arg2,
result, extra)`. Operands are one of three kinds:

- **`Const(value, type)`** — a compile-time-known literal.
- **`TempVar(id, type)`** — a compiler-generated temporary (`t0`, `t1`, ...), identity is
  `(name, type)`.
- **`Var(name, type, storage, scope_id)`** — a named source variable. Identity is **all four
  fields**, not just the name.

### `Var` identity: the `(name, storage, scope, type)` tuple

This is the single most important invariant in the whole backend, and the one most likely to bite a
future change: **every reference to what a human would call "the same variable" must construct an
identical `Var` tuple**, or the allocator treats them as two unrelated variables — splits them
across different registers/slots — and a value assigned through one reference silently never
reaches the other. Two real, historical bugs were exactly this:

- An array's `def_arr` (declaration) built its `Var` with `storage="global"` hardcoded, while every
  `read`/`write` of that array resolved storage from the actual declaring scope. An initialized
  array's literal bytes got baked at one data address while reads looked at a different one. Fixed
  by making `visit_ArrayDefinerNode` use `current_scope.storage`, matching every other reference.
- An assignment's `Var` used to take its **type from the RHS expression** instead of the variable's
  own declared type — so `int w = byteArr[i];` built the assignment's `Var` as `w:byte` (from the
  byte-array element's type) while every later use of `w` built `w:int` (its declared type). Two
  different `Var`s, two different registers, the assigned value never reached the later reads (read
  back as 0). Fixed: `EqualizeNode`'s `Var` always uses the variable's **declared** type
  (`node.type = var_type`, set by the semantic analyzer's `visit_EqualizeNode`), never the RHS's.
  Regression test: `tests/test_byte_local.txt` (checksum 5300).

If you ever touch code that constructs a `Var` for an existing variable, use the **declared**
type/storage/scope — never re-derive it from context — and check both these test names still pass.

### Control-flow shape

`if`/`while`/`for` all lower to the same primitive pair: a boolean temp, an `if <bool> -> label`
(branch-if-true) instruction, and `goto`/`label` for the rest — there's no separate "conditional
jump on comparison" TAC op; comparisons (`<`,`==`, ...) are themselves TAC ops that produce a 0/1
`bool` result consumed by an ordinary `if`. The backend's own fusion pass (below) is what turns the
common `<cmp> -> if` pair back into a single real branch instruction — the IR itself doesn't assume
that optimization.

### `switch` lowering: a worked example

`switch` is the one construct that generates a genuinely different *shape* of IR — worth walking
through since it's the concrete example of several other mechanisms (label-as-data, an indirect
jump, a CFG edge list) that a future feature (e.g. function pointers) would reuse:

1. Two bounds checks (`idx < min`, `max < idx`), each an ordinary `<` + `if`, jumping to `default`
   (or the switch's end, if there's no `default`) on failure.
2. `n = idx - min; off = n << 1; base = addr_label(table); ea = base + off; target = load_ptr(ea)`
   — `addr_label` is a new op that means "load the address of this label" (an `LDI` of a fixup, not
   a real value); `load_ptr` reads the jump-table entry at that computed address.
3. `goto_reg target` — an **indirect jump**: `arg1` is the register holding the target address, and
   `extra` carries the *list of every label this instruction could jump to* (every case label plus
   the default/end), purely so the liveness/CFG pass can model the edges correctly — a plain `goto`
   only ever has one target, so a new op was needed rather than overloading `goto`.
4. `jump_table` — emits the actual address table as **inline data**, right after the indirect jump.
   It is never executed (the jump skips over it) — the backend just writes N raw words, each
   resolved as a label fixup through the existing fixup machinery (no new resolver needed).
5. Case bodies, each ending in an implicit `goto` to the switch's end (this is the "auto-break, no
   fallthrough" behavior from the language side).

Backend-side, `goto_reg`/`jump_table` are both `Optimizer.BARRIER_OPS` (never optimized across) and
CFG terminators in the liveness pass, and `addr_label`'s result is opaque to constant propagation
(it's an address, not a value) — see below for what those passes actually do.

Corresponding ISA feature: `JMP` (opcode `0x17`) gained a **register mode** via its existing
`lower_flag` bit — `lower_flag=0` is the original `pc = imm16`; `lower_flag=1` is `pc = R[reg1]`
(the indirect jump). This is the standing pattern for extending the ISA at all — see
[Byte/word and ISA quirks](#bytewordisa-quirks) below.

## Semantic analysis

Three phases over the whole merged AST (`SemanticAnalyzer.visit_ProgramNode`), in this order:

1. **Consts + structs** (`_collect_consts`, `_collect_structs`) — fold every `const` to an int and
   compute every struct's byte-packed field offsets, then **strip both node kinds from the AST
   entirely** — nothing downstream (TAC gen, codegen) ever sees a `ConstDefNode`/`StructDefNode`.
   Consts must reference only earlier consts (declaration order across the whole merged unit,
   includes first).
2. **Function signatures registered**, then **every top-level `var`/array/struct-instance visited**
   (so a function body can reference a global declared textually after it), then **every function
   body visited**.
3. Type checking throughout uses one rule (`_assignable`): same type, or both "numeric"
   (`int`/`byte` — see [language.md](language.md#numeric-mixing-and-truncation)). `bool` is
   deliberately excluded from "numeric," so it's never silently interchangeable with `int`/`byte`.

`address_taken` (the set of `(name, scope_id)` pairs with `&x` taken somewhere) is collected here and
handed to the backend — it's the single piece of information that forces the allocator to give a
variable a stack slot instead of a register (see below).

## Class monomorphization

`src/analyzer/class_expander.py` runs **before** the semantic analyzer, on the raw merged AST. For
each `new Class inst;`, it deep-copies the class's field declarations (renamed `inst__field`) and
method bodies (renamed `inst__method`, with every `self.x`/`self.m()` inside rewritten to
`inst__x`/`inst__m()`), and rewrites every external call site (`inst.method()`, `inst.field`,
`obj.sub.field`) to the mangled name — via one small AST-walking rewriter (`_Rewriter`, driven by a
`CHILD_ATTRS` table of which fields to recurse into per node type). After this pass, `ClassDefNode`/
`NewInstanceNode` are gone; the semantic analyzer, TAC generator, and backend see only ordinary
globals and free functions. This is why classes cost nothing at runtime and needed zero backend
changes — see [language.md](language.md#classes) for the language-level rules this pass enforces.

## The optimizer

Two passes, run to a fixpoint **per basic block** (`Optimizer.optimize`):

- **Constant folding** — `Const OP Const -> Const` for arithmetic/bitwise/shift/comparison ops
  whose result feeds a `TempVar`; removes the folding instruction and remembers the substitution
  (`self.folded`) so a later **cross-block** reference to that same temp (e.g. `ret t` — `ret` is a
  block barrier) can still be re-materialized as the literal after the block-local passes finish.
- **Constant propagation** — a **single forward pass** per block: `var = <const>` makes `var` a
  known constant for uses *after* that point in the same block, invalidated the moment `var` is
  reassigned to anything non-constant. This is deliberately position-aware. An earlier version built
  one block-wide map first and substituted it everywhere, which propagated a constant **backward**
  into uses that occurred *before* the assignment — miscompiling the ordinary pattern `y = v; v =
  55; return y` (returned `55` instead of the correct value, because the map didn't distinguish
  "before" from "after"). Regression test: `tests/test_constprop_order.txt`. If you ever touch this
  pass, the property to preserve is: a constant only ever flows to instructions **later in
  instruction order within the same block**, never earlier.

**Block boundaries** (`get_blocks`) are drawn at labels, at every `goto`/`if`, and at
`Optimizer.BARRIER_OPS` (`call`, `ret`, `func_begin/end`, `entry_begin/end`, `asm`, `goto_reg`,
`jump_table`) — both passes are strictly block-local, so neither one ever reasons across a branch,
a call, or an indirect jump. A few ops are explicitly **opaque** to constant propagation even within
a block (`print`, `asm`, `addrof`, `def_arr`, `addr_label`) — substituting a constant into `addrof`'s
operand, for instance, would be nonsensical (it needs the variable's identity/address, not a value).

## The register allocator

`src/backend/core/allocator.py` — **linear-scan**, one register-or-stack-slot **home for the whole
function** per variable (see [Known limitations](#known-limitations) for what that trades away).

- **Register pool: `[1, 2, 3, 4, 5]`** (`R1`-`R5`). `R0` is never in the pool — it's reserved
  entirely as the backend's scratch/accumulator register (below). `R6`/`R7` are the frame
  pointer/stack pointer and never allocated to a variable.
- **Globals and arrays are never register-allocated** — a global's "location" is always its fixed
  data-section address; an array's identity *is* its address. Only locals and parameters go through
  linear scan at all.
- **The first ≤3 parameters pre-occupy `R1`-`R3`** for their live range (matching the calling
  convention — the caller already put them there); a 4th+ parameter is pinned to a fixed positive
  `FP`-relative stack offset (`4 + (i-3)*2`, since the caller pushed it before the call) and never
  competes for a register at all.
- **`&x` anywhere forces `x` onto the stack**, checked *before* the linear scan runs, and that slot
  is never recycled by another variable for the rest of the function (its address must stay valid
  as long as any pointer to it could still be read). A register-passed parameter whose address is
  taken is a special case: it's reclassified from "lives in `R1`-`R3`" to "lives on the stack," and
  the **prologue** spills the still-live incoming register value into that slot
  (`LocationMap.spilled_params`, a list of `(incoming_register, fp_offset)` pairs) before anything
  else in the function runs — steps 1-3 of prologue emission (set FP, push callee-saved, allocate
  the frame) deliberately never touch `R1`-`R3`, so the original argument values are still intact
  when this spill happens.
- **Spill slot layout below `FP`**: callee-saved `R4`/`R5` (if used) first, then spill slots, each
  2 bytes, packed downward. The exact offsets aren't computed until *after* the whole scan finishes
  (they depend on how many of `R4`/`R5` ended up used), so slots are assigned placeholder indices
  during the scan and resolved to real `FP`-relative offsets in a final pass.

## Codegen emission: operand-aware addressing

This is where most of the ISA-level "quirks" live. The core idea (`_operand_form`/`_emit_op_rhs`,
`_home_reg`, `_emit_to_reg`/`_emit_from_reg`): **the second operand of an ALU/CMP op is never loaded
into a scratch register** — it's addressed *in place*, in whichever of four forms it actually lives
in:

| Operand lives in | Form | Encoding |
|---|---|---|
| A constant | `imm` | I-type: `OP reg, #imm16` (4 B) |
| A register | `reg` | R-type: `OP reg, reg2` (2 B) |
| A stack slot | `stk` | M-type, FP-relative: `OP reg, [FP+off]` (4 B) |
| A global | `abs` | **M-type absolute**: `OP reg, [#addr]` (4 B) |

Only the **first** operand is ever moved into an accumulator register — because the ALU instruction
format requires its first operand to be a register (it's overwritten in place by the result). Which
register: if the result lives in a register, operate directly into that register (loading arg1 there
first, unless arg1 is *already* there — e.g. `i = i + 1` emits straight into `i`'s register with no
extra move at all); if the result is stack/global, compute in `R0` and store out afterward.

**M-type absolute addressing** is a real ISA feature, not just a backend trick: bit 1 of the
instruction word (`ABS_FLAG` in `emu_isa.py`) means "the trailing imm16 IS the address, ignore the
base register," decoded in the CPU core's ALU execution, `CMP`, and `LDROFF`. Before this existed,
reading a global required `LDI R3,addr; LDR reg,[R3]` — burning a scratch register (often a live
argument register) just to hold an address that's known at compile time. The absolute-addressing
bit removes that: `_emit_ldroff_abs`/`_emit_m_abs` load or operate on a global directly in one
4-byte instruction, no register clobbered. (`STROFF` has no absolute-flag mode — an absolute *store*
already had a dedicated opcode, `STRI`, before this feature existed.)

Other specific emission behaviors worth knowing:

- **Copy elision** (`def`/`eq`, i.e. `x = y`): same register → nothing emitted; same stack slot
  (two variables the allocator happened to coalesce onto one slot without overlapping) → nothing
  emitted; register↔register/stack/global → the minimum one or two instructions; only a genuine
  stack↔stack or global↔global copy bounces through `R0`.
- **The one non-commutative aliasing case** (`x = y - x`, i.e. the second operand already occupies
  the *destination* register): computed in `R0` first, then moved into place, since overwriting the
  destination register with `arg1` before reading `arg2` from it would read a value that's already
  been clobbered. Commutative ops (`+ & | ^ *`) don't have this problem — `res = arg2 OP arg1` is
  reordered instead, no extra instruction needed.
- **Self-base pointer loads** (`load_ptr`, i.e. `*p`): the pointer's *value* is loaded directly into
  the destination register, then that same register is used as both the load's base and destination
  (`LDR rd, [rd]`) — well-defined because the CPU core reads the base into a local before writing
  the destination register, so a self-referencing load never sees a partially-updated value. A byte
  load additionally zero-extends with a post-load `AND reg, #0xFF` (see
  [Byte/word and ISA quirks](#bytewordisa-quirks)).
- **`store_ptr`** uses the pointer's register directly as the store's base register whenever it has
  one; the only case that needs a guarded scratch register (`R4`, push/pop-wrapped) is when *both*
  the pointer and the value being stored are spilled/non-register — genuinely rare.

### Compare+branch fusion

A comparison (`< <= > >= == !=`) whose boolean result is consumed by nothing except the
*immediately following* `if`, and is dead after that (`_precompute_marks`, using per-function
liveness computed up front): instead of materializing a real `0`/`1` value and then comparing it
against `0` again in the `if`, the backend emits the `CMP` once and branches **directly** to the
`if`'s real target using the appropriate flag-based jump (`JS`/`JZ`/`JNZ`/`JNS`, or a two-jump
sequence for `<=`/`>`, which need "sign OR zero" / "not sign AND not zero"). This is why almost
every `if`/`while`/`for` condition in compiled output is one `CMP` + one branch, not four or five
instructions — it's the single biggest measured win in the whole operand-aware/fusion effort
(loop-heavy code measured 22-32% fewer executed instructions vs. the pre-fusion baseline).

### Call-site register spilling and argument staging

- **Spilling**: only the caller-saved registers (`R1`-`R3`) that hold a value **live across the
  call** (from the precomputed liveness, not a blanket "always spill all three") are pushed before
  the call and popped after.
- **Argument staging**: arguments load **directly** into `R1`-`R3` in the common case. The only
  reason to fall back to the safe-but-heavier push-everything-then-pop-into-place sequence is a
  genuine clobber hazard — some argument's *source value* already lives in one of `R1`-`R3` (e.g.
  `f(b, a)` where `a`/`b` are already sitting in the argument registers in the wrong order); loading
  args in place in that case could overwrite a value before it's been read. Constants, stack values,
  globals, and `R4`/`R5`-resident values never conflict and always load directly.
- Both together replace what used to be an unconditional, ~12-instruction-per-3-arg-call sequence
  with something close to the theoretical minimum whenever there's no real hazard.

### Dead-store elimination (DSE)

A pure, side-effect-free instruction (`+ - * / | & ^ shl shr < <= > >= == != def eq addrof`) whose
result is never read again (per-function liveness) is skipped entirely at emission. **Deliberately
excluded**: anything writing a **global** (another function might read it — per-function liveness
can't see that) or an **address-taken local** (a later `*p` read reaches it through the pointer, not
through the name, so per-function liveness can't see that either), and anything with a genuine
side effect (`call`, `store_ptr`, `load_ptr` — a load may touch memory-mapped I/O, so even an
apparently-unused load must still run). This pass exists as much for **correctness** as for
performance: the allocator can legally give two non-overlapping variables the same register, and a
store to one of them *after* its last real use (a dead store) would otherwise silently corrupt the
other variable if that store still executed. Skipping dead stores makes that corruption
structurally impossible — see [Known limitations](#known-limitations) for the deeper allocator
property this papers over.

## Byte/word/ISA quirks

- **Every register is a 16-bit word**; a `byte`-typed *array element or struct field* is the only
  place an actual 1-byte-wide memory access happens (`size_flag`/`lower_flag` bits select the 1-byte
  `LDR`/`STR` encoding). A byte load always **zero-extends** afterward — there is no sign-extending
  byte load in the ISA, so a `byte` is always effectively unsigned once it's in a register.
- **A byte *store* into an array must match the array's declared element type**, not the type of
  the value expression being stored — storing a plain `int` constant into a `byte[]` element still
  emits a 1-byte store (the TAC generator carries the destination's element type explicitly on
  `store_ptr`'s `extra` field precisely so the backend doesn't guess wrong and accidentally do a
  2-byte store that clobbers an adjacent byte).
- **Struct field packing**: `int`/`bool` fields cost 2 bytes, `byte` fields cost 1, no padding
  between fields, offsets assigned in declaration order — see [language.md](language.md#structs).
- **The 5-bit opcode field is completely full**: 32 opcode slots (`0x00`-`0x1F`), all 32 in use
  (`src/backend/emu_isa.py`; `MUL`/`DIV` are the last two, `0x1E`/`0x1F`). **There is no room for a
  new top-level instruction.** Every extension so far has reused an existing opcode plus a spare
  per-instruction header bit to select a new addressing/execution *mode* on that same opcode — the
  ALU ops already use `lower_flag` to choose R-type-vs-I-type, `JMP` gained its register-jump mode
  the same way (an unused header bit, not a new opcode), and M-type absolute addressing is the
  `ABS_FLAG` bit. If a future feature needs new CPU behavior, look for a spare bit on an existing
  opcode first — there is no free opcode slot to claim.

## Known limitations

Documented so a future session doesn't have to rediscover these by profiling or debugging:

- **One register-or-stack home per variable for the whole function** (no live-range splitting across
  "holes" in a variable's use pattern). This was investigated and **measured not worth fixing**: per
  function, the true peak simultaneous-live-variable count matches the peak of naively-overlapping
  `[first_def, last_use]` intervals almost everywhere in real code (checked against actual compiled
  functions) — meaning register pressure, not fragmentation, is the actual constraint, and splitting
  live ranges wouldn't reduce spilling in practice. Not planned.
- **Disjoint live-range / SSA-style allocation** was considered as the "principled" fix for the
  above and for a similar historical dead-store aliasing question — same conclusion: skip it, the
  payoff doesn't justify a full allocator rewrite (DSE alone already closes the correctness gap;
  see above).
- **The optimizer is strictly per-basic-block** (constant folding and propagation never cross a
  branch, call, or indirect jump) — this is intentional conservatism, not an oversight, but it does
  mean the same constant re-computed on every loop iteration inside a loop body won't be hoisted;
  there's no loop-invariant code motion.
- **A non-`void` function that falls off the end without hitting `return` on every path is not
  statically rejected** by the semantic analyzer — whatever happens to be in `R0` at that point
  becomes the return value. Write an explicit `return` on every path.
