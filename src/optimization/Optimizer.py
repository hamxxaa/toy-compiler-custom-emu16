from codegen.TACGenerator import *
import warnings


class Optimizer:
    def __init__(self):
        self.folded = {}

    # These ops define semantic boundaries where cross-instruction propagation
    # is unsafe for a local optimizer (function/entry boundaries and calls).
    BARRIER_OPS = {
        "call",
        "ret",
        "func_begin",
        "func_end",
        "entry_begin",
        "entry_end",
        "asm",
    }

    @staticmethod
    def wrap_to_32bit(value):
        INT32_MIN = -2147483648
        INT32_MAX = 2147483647

        if value < INT32_MIN or value > INT32_MAX:
            warnings.warn(
                f"Integer overflow in constant folding: {value} wraps to 32-bit range. "
                f"This matches runtime x86 behavior but may indicate a bug.",
                RuntimeWarning,
                stacklevel=3,
            )

        wrapped = value % (2**32)

        if wrapped >= 2**31:
            wrapped -= 2**32

        return wrapped

    def optimize(self, TAC):
        # Maps temps that were folded away (and their producing instruction removed)
        # to their constant value, so cross-block uses (e.g. `ret t`, where `ret` is a
        # block barrier) can be re-materialized after the per-block passes.
        self.folded = {}
        blocks = self.get_blocks(TAC.instructions)
        for block in blocks:
            changed = True
            while changed:
                f = self.constant_folding(block)
                p = self.constant_propagation(block)
                changed = f or p
        TAC.instructions = [instr for block in blocks for instr in block]

        # Global re-materialization: any remaining reference to a folded temp
        # (it escaped its block) becomes the constant directly.
        if self.folded:
            for instr in TAC.instructions:
                if instr.op == "asm":
                    continue
                if instr.arg1 in self.folded:
                    instr.arg1 = self.folded[instr.arg1]
                if instr.arg2 in self.folded:
                    instr.arg2 = self.folded[instr.arg2]

        TAC.line_count = len(TAC.instructions)

    def get_blocks(self, instructions):
        leader_indices = set()

        if instructions:
            leader_indices.add(0)

        label_map = {
            instr.result: i
            for i, instr in enumerate(instructions)
            if instr.op == "label"
        }

        for i, instr in enumerate(instructions):
            if instr.op in ["goto", "if"]:
                if instr.result in label_map:
                    leader_indices.add(label_map[instr.result])

                if i + 1 < len(instructions):
                    leader_indices.add(i + 1)

            if instr.op in self.BARRIER_OPS:
                leader_indices.add(i)
                if i + 1 < len(instructions):
                    leader_indices.add(i + 1)

        sorted_leaders = sorted(list(leader_indices))

        blocks = []
        for i, start_index in enumerate(sorted_leaders):
            if i + 1 < len(sorted_leaders):
                end_index = sorted_leaders[i + 1]
            else:
                end_index = len(instructions)

            block = instructions[start_index:end_index]
            blocks.append(block)

        return blocks

    def constant_folding(self, instrs):
        def spread_constants(instrs, constant_map):
            changed = False
            for instr in instrs:
                if instr.op == "asm":
                    continue
                if instr.arg1 in constant_map:
                    changed = True
                    instr.arg1 = Const(constant_map[instr.arg1], type=instr.arg1.type)
                if instr.arg2 in constant_map:
                    changed = True
                    instr.arg2 = Const(constant_map[instr.arg2], type=instr.arg2.type)
            return changed

        temp_map = {}
        changed = False

        to_remove = []
        for instr in instrs:
            if instr.op in [
                "+",
                "-",
                "*",
                "/",
                "|",
                "&",
                "^",
                "<",
                "<=",
                ">",
                ">=",
                "==",
                "!=",
                "shl",
                "shr",
            ]:
                if (
                    isinstance(instr.arg1, Const)
                    and isinstance(instr.arg2, Const)
                    and isinstance(instr.result, TempVar)
                ):
                    changed = True
                    if instr.op == "+":
                        constant = self.wrap_to_32bit(
                            instr.arg1.value + instr.arg2.value
                        )
                    elif instr.op == "-":
                        constant = self.wrap_to_32bit(
                            instr.arg1.value - instr.arg2.value
                        )
                    elif instr.op == "*":
                        constant = self.wrap_to_32bit(
                            instr.arg1.value * instr.arg2.value
                        )
                    elif instr.op == "/":
                        if instr.arg2.value == 0:
                            raise ZeroDivisionError(
                                "Division by zero in constant folding"
                            )
                        constant = int(instr.arg1.value / instr.arg2.value)
                    elif instr.op == "&":
                        constant = int(instr.arg1.value) & int(instr.arg2.value)
                    elif instr.op == "|":
                        constant = int(instr.arg1.value) | int(instr.arg2.value)
                    elif instr.op == "^":
                        constant = int(instr.arg1.value) ^ int(instr.arg2.value)
                    elif instr.op == "<":
                        constant = 1 if int(instr.arg1.value < instr.arg2.value) else 0
                    elif instr.op == "<=":
                        constant = 1 if int(instr.arg1.value <= instr.arg2.value) else 0
                    elif instr.op == ">":
                        constant = 1 if int(instr.arg1.value > instr.arg2.value) else 0
                    elif instr.op == ">=":
                        constant = 1 if int(instr.arg1.value >= instr.arg2.value) else 0
                    elif instr.op == "==":
                        constant = 1 if int(instr.arg1.value == instr.arg2.value) else 0
                    elif instr.op == "!=":
                        constant = 1 if int(instr.arg1.value != instr.arg2.value) else 0
                    elif instr.op == "shl":
                        constant = self.wrap_to_32bit(int(instr.arg1.value) << (int(instr.arg2.value) & 0xF))
                    elif instr.op == "shr":
                        constant = self.wrap_to_32bit(int(instr.arg1.value) >> (int(instr.arg2.value) & 0xF))
                    temp_map[instr.result] = constant
                    self.folded[instr.result] = Const(constant, type=instr.result.type)
                    to_remove.append(instr)

        if to_remove:
            changed = True
            for instr in to_remove:
                instrs.remove(instr)

        c = spread_constants(instrs, temp_map)
        return changed or c

    def constant_propagation(self, instrs):
        # SINGLE FORWARD PASS over the block. A `var = <const>` assignment makes that variable a
        # known constant only for uses that come AFTER it, and only until the variable is
        # reassigned. The previous version built one block-wide map first and then substituted it
        # into every use — including uses BEFORE the assignment — which propagated a constant
        # BACKWARD and miscompiled e.g. `y = v; v = 5; return y` (y wrongly became 5).
        # (Cross-block leakage is already prevented: get_blocks splits on control flow, calls, and
        # other barriers, so propagation never crosses a branch or a call that could change a var.)
        var_map = {}   # Var -> current known constant value (valid from here forward in this block)
        changed = False
        # Ops whose operands must NOT be const-substituted: addrof needs the variable's address
        # (not its value); print/asm/def_arr are opaque to the optimizer.
        OPAQUE = ("print", "asm", "addrof", "def_arr")
        for instr in instrs:
            # 1. Substitute already-known constants into this instruction's operands.
            if instr.op not in OPAQUE:
                if instr.arg1 in var_map:
                    instr.arg1 = Const(var_map[instr.arg1], type=instr.arg1.type)
                    changed = True
                if instr.arg2 in var_map:
                    instr.arg2 = Const(var_map[instr.arg2], type=instr.arg2.type)
                    changed = True
            # 2. Update the map from this instruction's definition (after substitution above, so a
            #    copy of a known constant — `b = a` with a known — also becomes known).
            if isinstance(instr.result, Var):
                if instr.op in ("eq", "def") and isinstance(instr.arg1, Const):
                    var_map[instr.result] = instr.arg1.value     # now a known constant
                else:
                    var_map.pop(instr.result, None)              # reassigned to a non-constant
        return changed
