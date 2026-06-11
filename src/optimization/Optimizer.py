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
                        constant = instr.arg1.value and instr.arg2.value
                    elif instr.op == "|":
                        constant = instr.arg1.value or instr.arg2.value
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
        def spread_vars(instrs, var_map, when_to_del):
            changed = False
            for i, instr in enumerate(instrs):
                if instr.op in ("print", "asm", "addrof", "def_arr"):
                    # addrof: replacing a Var with its constant value would be wrong
                    #   (we need the *address*, not the value stored at that address).
                    # def_arr: arg1/arg2 are a string/int, not operands — skip safely.
                    continue

                if instr.arg1 in var_map and (
                    i <= when_to_del[instr.arg1]
                    if when_to_del.get(instr.arg1)
                    else True
                ):

                    changed = True
                    instr.arg1 = Const(var_map[instr.arg1], type=instr.arg1.type)
                if instr.arg2 in var_map and (
                    i <= when_to_del[instr.arg2]
                    if when_to_del.get(instr.arg2)
                    else True
                ):
                    changed = True
                    instr.arg2 = Const(var_map[instr.arg2], type=instr.arg2.type)
            return changed

        var_map = {}
        when_to_del = {}
        for i, instr in enumerate(instrs):
            if instr.op in ["eq", "def"] and isinstance(instr.result, Var):
                if isinstance(instr.arg1, Const):
                    var_map[instr.result] = instr.arg1.value
                elif isinstance(instr.arg1, Var) and instr.arg1 in var_map:
                    var_map[instr.result] = var_map[instr.arg1]
                else:
                    if instr.result in var_map:
                        when_to_del[instr.result] = i

        c = spread_vars(instrs, var_map, when_to_del)
        return c
