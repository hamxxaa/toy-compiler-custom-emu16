class TACInstruction:
    def __init__(self, op, arg1=None, arg2=None, result=None, extra=None):
        self.op = op
        self.arg1 = arg1
        self.arg2 = arg2
        self.result = result
        self.extra = extra      # opaque payload (e.g. array-literal init values for def_arr)

    def __str__(self):
        parts = [self.op]
        if self.arg1 is not None:
            parts.append(str(self.arg1))
        if self.arg2 is not None:
            parts.append(str(self.arg2))
        if self.result is not None:
            parts.append(str(self.result))
        return " ".join(parts)


class Operand:
    pass


class TempVar(Operand):
    def __init__(self, id, type):
        self.id = id
        self.name = f"t{id}"
        self.type = type

    def __str__(self):
        return f"{self.name} ({self.type})"

    def __eq__(self, other):
        return (
            isinstance(other, TempVar)
            and self.name == other.name
            and self.type == other.type
        )

    def __hash__(self):
        return hash((self.name, self.type))

class Var(Operand):
    def __init__(self, name, type, storage="global", scope_id=0):
        self.name = name
        self.type = type
        self.storage = storage
        self.scope_id = scope_id

    def __str__(self):
        if self.storage == "local":
            return f"{self.name}_s{self.scope_id} ({self.type}, {self.storage})"
        return f"{self.name} ({self.type}, {self.storage})"

    def __eq__(self, other):
        return (
            isinstance(other, Var)
            and self.name == other.name
            and self.type == other.type
            and self.storage == other.storage
            and self.scope_id == other.scope_id
        )

    def __hash__(self):
        return hash((self.name, self.type, self.storage, self.scope_id))

class Const(Operand):
    def __init__(self, value, type):
        self.value = value
        self.type = type

    def __str__(self):
        return f"{self.value} ({self.type})"

    def __eq__(self, other):
        return (
            isinstance(other, Const)
            and self.value == other.value
            and self.type == other.type
        )

    def __hash__(self):
        return hash((self.value, self.type))


class TAC:
    def __init__(self, instructions):
        self.instructions = instructions
        self.line_count = len(instructions)


class TACGenerator:
    def __init__(self):
        self.instructions = []
        self.temp_count = 0
        self.label_count = 0
        self.current_function = None
        self.loops = []   # stack of (continue_label, break_label) for break/continue

    def generate_tac(self, ast):
        self.generate(ast)
        return TAC(self.instructions)

    def new_temp(self, type):
        temp = TempVar(self.temp_count, type)
        self.temp_count += 1
        return temp

    def new_label(self):
        label_name = f"L{self.label_count}"
        self.label_count += 1
        return label_name

    def create_instruction(self, op, arg1=None, arg2=None, result=None, extra=None):
        instruction = TACInstruction(op, arg1, arg2, result, extra)
        self.instructions.append(instruction)
        return instruction

    def generate(self, node):
        method_name = f"visit_{type(node).__name__}"
        visitor = getattr(self, method_name, self.generic_visit)
        return visitor(node)

    def generic_visit(self, node):
        raise Exception(f"No visit_{type(node).__name__} method")

    def visit_ProgramNode(self, node):
        top_level_declarations = node.scope.statements
        global_definitions = []
        function_definitions = []

        for declaration in top_level_declarations:
            node_name = type(declaration).__name__
            if node_name in ("DefinerNode", "ArrayDefinerNode", "StructVarNode"):
                global_definitions.append(declaration)
            elif node_name == "FunctionDefNode":
                function_definitions.append(declaration)
            else:
                raise Exception(
                    f"Codegen Error: Unsupported top-level declaration '{node_name}'."
                )

        # Entry block: evaluate global initializers and call main.
        self.create_instruction("entry_begin")
        for definition in global_definitions:
            self.generate(definition)
        main_result = self.new_temp("int")
        self.create_instruction("call", arg1="main", arg2=0, result=main_result)
        self.create_instruction("entry_end")

        for function in function_definitions:
            self.generate(function)

    def visit_ScopeNode(self, node):
        for statement in node.statements:
            self.generate(statement)

    def visit_FunctionDefNode(self, node):
        previous_function = self.current_function
        self.current_function = node

        self.create_instruction("func_begin", arg1=node.name, arg2=node.return_type)
        for param in node.params:
            self.create_instruction("param", arg1=f"{param.name}:{param.type}")

        self.generate(node.scope)

        # Naked functions supply their own RET inside the asm block; don't auto-add one.
        if node.return_type == "void" and not getattr(node, "is_naked", False):
            if not self.instructions or self.instructions[-1].op != "ret":
                self.create_instruction("ret")

        self.create_instruction("func_end", arg1=node.name)
        self.current_function = previous_function

    def visit_FunctionCallNode(self, node):
        for arg in node.args:
            evaluated_arg = self.generate(arg)
            self.create_instruction("arg", arg1=evaluated_arg)

        if node.type == "void":
            self.create_instruction("call", arg1=node.name, arg2=len(node.args))
            return None

        result = self.new_temp(node.type)
        self.create_instruction("call", arg1=node.name, arg2=len(node.args), result=result)
        return result

    def visit_ReturnNode(self, node):
        return_value = self.generate(node.expression) if node.expression else None
        self.create_instruction("ret", arg1=return_value)

    def visit_DefinerNode(self, node):
        value = self.generate(node.value) if node.value else None
        if isinstance(value, Const):
            self.create_instruction(
                "def", arg1=value, result=Var(node.name, type=node.type, storage=node.storage, scope_id=node.scope_id)
            )
        elif value is not None:
            self.create_instruction(
                "def", result=Var(node.name, type=node.type, storage=node.storage, scope_id=node.scope_id)
            )
            self.create_instruction(
                "eq", arg1=value, result=Var(node.name, type=node.type, storage=node.storage, scope_id=node.scope_id)
            )
        else:
            self.create_instruction(
                "def", result=Var(node.name, type=node.type, storage=node.storage, scope_id=node.scope_id)
            )

    def visit_EqualizeNode(self, node):
        value = self.generate(node.value)
        # Use the variable's DECLARED type (set by the analyzer), not the RHS expression's type, so
        # this Var is identical to every other reference to the same variable. Mismatched types make
        # the allocator treat them as distinct vars (e.g. `int w = byteArr[i]` -> w:byte vs w:int),
        # splitting them across registers so the value never propagates.
        self.create_instruction(
            "eq", arg1=value, result=Var(node.name, type=node.type, storage=node.storage, scope_id=node.scope_id)
        )

    def visit_IfNode(self, node):
        condition = self.generate(node.condition)
        then_label = self.new_label()
        self.create_instruction("if", arg1=condition, result=then_label)
        if node.else_body is None:
            end_label = self.new_label()
            self.create_instruction("goto", result=end_label)
            self.create_instruction("label", result=then_label)
            self.generate(node.scope)
            self.create_instruction("label", result=end_label)
        else:
            else_label = self.new_label()
            end_label = self.new_label()
            self.create_instruction("goto", result=else_label)
            self.create_instruction("label", result=then_label)
            self.generate(node.scope)
            self.create_instruction("goto", result=end_label)
            self.create_instruction("label", result=else_label)
            self.generate(node.else_body)
            self.create_instruction("label", result=end_label)

    def visit_WhileNode(self, node):
        start_label = self.new_label()
        self.create_instruction("label", result=start_label)
        condition = self.generate(node.condition)
        mid_label = self.new_label()
        end_label = self.new_label()
        self.create_instruction("if", arg1=condition, result=mid_label)
        self.create_instruction("goto", result=end_label)
        self.create_instruction("label", result=mid_label)
        self.loops.append((start_label, end_label))   # continue -> re-test, break -> exit
        self.generate(node.scope)
        self.loops.pop()
        self.create_instruction("goto", result=start_label)
        self.create_instruction("label", result=end_label)

    def visit_ForNode(self, node):
        if node.init is not None:
            self.generate(node.init)
        start_label = self.new_label()
        mid_label = self.new_label()
        post_label = self.new_label()
        end_label = self.new_label()
        self.create_instruction("label", result=start_label)
        condition = self.generate(node.condition)
        self.create_instruction("if", arg1=condition, result=mid_label)
        self.create_instruction("goto", result=end_label)
        self.create_instruction("label", result=mid_label)
        self.loops.append((post_label, end_label))     # continue -> post-update, break -> exit
        self.generate(node.scope)
        self.loops.pop()
        self.create_instruction("label", result=post_label)
        if node.post is not None:
            self.generate(node.post)
        self.create_instruction("goto", result=start_label)
        self.create_instruction("label", result=end_label)

    def visit_SwitchNode(self, node):
        # O(1) jump-table dispatch (see the plan): bounds-check -> index a table of case-body
        # addresses -> indirect jump. Constant work regardless of case count.
        idx = self.generate(node.expr)
        values = node.case_values                       # folded ints, aligned with node.cases
        labels = [self.new_label() for _ in values]
        min_v, max_v = min(values), max(values)

        end_label = self.new_label()
        has_default = node.default is not None
        default_label = self.new_label() if has_default else None
        eff_default = default_label if has_default else end_label   # holes / out-of-range land here
        tbl_label = self.new_label()

        # Bounds check via two signed '<' tests (each fuses to CMP+JS in the backend):
        #   idx < min  -> default ;  max < idx (i.e. idx > max) -> default
        lo = self.new_temp("bool")
        self.create_instruction("<", arg1=idx, arg2=Const(min_v, "int"), result=lo)
        self.create_instruction("if", arg1=lo, result=eff_default)
        hi = self.new_temp("bool")
        self.create_instruction("<", arg1=Const(max_v, "int"), arg2=idx, result=hi)
        self.create_instruction("if", arg1=hi, result=eff_default)

        # target = *(&table + (idx - min) * 2)
        n = self.new_temp("int")
        self.create_instruction("-", arg1=idx, arg2=Const(min_v, "int"), result=n)
        off = self.new_temp("int")
        self.create_instruction("shl", arg1=n, arg2=Const(1, "int"), result=off)
        base = self.new_temp("int")
        self.create_instruction("addr_label", arg1=tbl_label, result=base)   # LDI base, &table
        ea = self.new_temp("int")
        self.create_instruction("+", arg1=base, arg2=off, result=ea)
        tgt = self.new_temp("int")
        self.create_instruction("load_ptr", arg1=ea, result=tgt)

        # Indirect jump. `extra` lists every reachable target so the CFG (liveness) models the edges.
        label_by_value = {v: l for v, l in zip(values, labels)}
        targets = list(labels)
        targets.append(eff_default)
        self.create_instruction("goto_reg", arg1=tgt, extra=targets)

        # The address table: one word per index min..max, holes -> default. Emitted as data right
        # after the indirect jump (never executed — the jump skips over it to a case body).
        table = [label_by_value.get(v, eff_default) for v in range(min_v, max_v + 1)]
        self.create_instruction("jump_table", arg1=tbl_label, extra=table)

        # Case bodies (auto-break: each falls to the switch end).
        for (value_expr, body), label in zip(node.cases, labels):
            self.create_instruction("label", result=label)
            self.generate(body)
            self.create_instruction("goto", result=end_label)
        if has_default:
            self.create_instruction("label", result=default_label)
            self.generate(node.default)
        self.create_instruction("label", result=end_label)

    def visit_BreakNode(self, node):
        if not self.loops:
            raise Exception("Codegen Error: 'break' outside a loop.")
        self.create_instruction("goto", result=self.loops[-1][1])

    def visit_ContinueNode(self, node):
        if not self.loops:
            raise Exception("Codegen Error: 'continue' outside a loop.")
        self.create_instruction("goto", result=self.loops[-1][0])

    def visit_PrintNode(self, node):
        expression_result = self.generate(node.expression)
        self.create_instruction("print", arg1=expression_result)

    def visit_AsmNode(self, node):
        # Carry the raw asm lines through to the backend's mini-assembler.
        self.create_instruction("asm", arg1=node.lines)

    def visit_ConditionNode(self, node):
        left = self.generate(node.left)
        right = self.generate(node.right)
        temp = self.new_temp(type=node.type)
        # Bools are 0/1 words, so logical && / || lower to the bitwise ALU ops.
        op = {"&&": "&", "||": "|"}.get(node.operator, node.operator)
        self.create_instruction(op, arg1=left, arg2=right, result=temp)
        return temp

    def visit_ExpressionNode(self, node):
        left = self.generate(node.left)
        right = self.generate(node.right)
        temp = self.new_temp(type=node.type)
        # Backend speaks "shl"/"shr" for shifts.
        op = {"<<": "shl", ">>": "shr"}.get(node.operator, node.operator)
        self.create_instruction(op, arg1=left, arg2=right, result=temp)
        return temp

    def visit_BitNotNode(self, node):
        # ~x  ==  x ^ 0xFFFF  (16-bit one's complement; no NOT opcode in the ISA)
        inner = self.generate(node.inner)
        temp = self.new_temp(type=node.type)
        self.create_instruction("^", arg1=inner, arg2=Const(0xFFFF, "int"), result=temp)
        return temp

    def visit_NegNode(self, node):
        # -x  ==  0 - x   (reuses the subtract op; no unary-negate opcode needed)
        inner = self.generate(node.inner)
        temp = self.new_temp(type=node.type)
        self.create_instruction("-", arg1=Const(0, "int"), arg2=inner, result=temp)
        return temp

    def visit_TermNode(self, node):
        left = self.generate(node.left)
        right = self.generate(node.right)
        if node.operator == "%":
            # a % b  ==  a - (a / b) * b   (truncated; no MOD opcode, signed div/mul/sub exist)
            q = self.new_temp("int")
            self.create_instruction("/", arg1=left, arg2=right, result=q)
            m = self.new_temp("int")
            self.create_instruction("*", arg1=q, arg2=right, result=m)
            temp = self.new_temp(type=node.type)
            self.create_instruction("-", arg1=left, arg2=m, result=temp)
            return temp
        temp = self.new_temp(type=node.type)
        self.create_instruction(node.operator, arg1=left, arg2=right, result=temp)
        return temp

    def visit_FactorNode(self, node):
        if node.is_variable:
            return Var(node.value, type=node.type, storage=node.storage, scope_id=node.scope_id)
        else:
            # Handle boolean and integer constants properly
            if node.type == "bool":
                if node.value.lower() == "true":
                    value = 1
                elif node.value.lower() == "false":
                    value = 0
                else:
                    raise ValueError(f"Invalid boolean value: {node.value}")
            else:
                # int(..., 0) handles both decimal and 0x hex literals
                value = int(node.value, 0) if isinstance(node.value, str) else int(node.value)
                if value < -2147483648 or value > 2147483647:
                    raise ValueError(
                        f"Integer constant out of bounds (32-bit): {value}"
                    )
            return Const(value, type=node.type)

    # ── M2: Arrays ──────────────────────────────────────────────────────────
    def visit_ArrayDefinerNode(self, node):
        """Reserve data-section storage for an array; no runtime code emitted."""
        arr_var = Var(node.name, type=node.elem_type + "[]",
                      storage=node.storage, scope_id=node.scope_id)
        self.create_instruction("def_arr",
                                arg1=node.elem_type,
                                arg2=node.size,
                                result=arr_var,
                                extra=getattr(node, "init_values", None))

    # ── D6: Structs ───────────────────────────────────────────────────────────
    def visit_StructVarNode(self, node):
        """Reserve sizeof(struct)*count bytes in the data section (no runtime code)."""
        decl_type = node.struct_name + ("[]" if node.is_array else "")
        var = Var(node.name, type=decl_type, storage=node.storage, scope_id=node.scope_id)
        self.create_instruction("def_arr", arg1="byte", arg2=node.size_bytes, result=var)

    def _member_address(self, node):
        """Temp holding the address of node's field: addrof(base) [+ index*stride] [+ field_offset]."""
        var = Var(node.name, type=node.decl_type, storage=node.storage, scope_id=node.scope_id)
        addr = self.new_temp("int")
        self.create_instruction("addrof", arg1=var, result=addr)
        if node.index is not None:
            idx = self.generate(node.index)
            off = self.new_temp("int")
            self.create_instruction("*", arg1=idx, arg2=Const(node.stride, "int"), result=off)
            ea = self.new_temp("int")
            self.create_instruction("+", arg1=addr, arg2=off, result=ea)
            addr = ea
        if node.field_offset != 0:
            fa = self.new_temp("int")
            self.create_instruction("+", arg1=addr, arg2=Const(node.field_offset, "int"), result=fa)
            addr = fa
        return addr

    def visit_MemberAccessNode(self, node):
        addr = self._member_address(node)
        result = self.new_temp(node.field_type)   # byte field -> byte load
        self.create_instruction("load_ptr", arg1=addr, result=result)
        return result

    def visit_MemberAssignNode(self, node):
        addr = self._member_address(node)
        val = self.generate(node.value)
        self.create_instruction("store_ptr", arg1=val, arg2=addr, extra=node.field_type)

    def visit_IndexNode(self, node):
        """arr[i] — compute address, emit load_ptr."""
        arr_var = Var(node.array_name, type=node.elem_type + "[]",
                      storage=node.storage, scope_id=node.scope_id)
        base_temp = self.new_temp("int")
        self.create_instruction("addrof", arg1=arr_var, result=base_temp)

        idx_temp = self.generate(node.index)
        elem_size = 2 if node.elem_type == "int" else 1

        if elem_size == 2:
            # offset = idx * 2 = idx << 1
            off_temp = self.new_temp("int")
            self.create_instruction("shl",
                                    arg1=idx_temp, arg2=Const(1, "int"),
                                    result=off_temp)
            addr_temp = self.new_temp("int")
            self.create_instruction("+", arg1=base_temp, arg2=off_temp, result=addr_temp)
        else:
            # byte array: offset = idx (no scaling)
            addr_temp = self.new_temp("int")
            self.create_instruction("+", arg1=base_temp, arg2=idx_temp, result=addr_temp)

        result_temp = self.new_temp(node.elem_type)
        self.create_instruction("load_ptr", arg1=addr_temp, result=result_temp)
        return result_temp

    def visit_IndexAssignNode(self, node):
        """arr[i] = val — compute address, emit store_ptr."""
        arr_var = Var(node.array_name, type=node.elem_type + "[]",
                      storage=node.storage, scope_id=node.scope_id)
        base_temp = self.new_temp("int")
        self.create_instruction("addrof", arg1=arr_var, result=base_temp)

        idx_temp = self.generate(node.index)
        elem_size = 2 if node.elem_type == "int" else 1

        if elem_size == 2:
            off_temp = self.new_temp("int")
            self.create_instruction("shl",
                                    arg1=idx_temp, arg2=Const(1, "int"),
                                    result=off_temp)
            addr_temp = self.new_temp("int")
            self.create_instruction("+", arg1=base_temp, arg2=off_temp, result=addr_temp)
        else:
            addr_temp = self.new_temp("int")
            self.create_instruction("+", arg1=base_temp, arg2=idx_temp, result=addr_temp)

        val_temp = self.generate(node.value)
        # Carry the destination element type so the backend emits a byte-wide store for
        # byte arrays (mirrors load_ptr, which keys off the loaded element type). Without
        # this, storing an int-typed value (e.g. a constant) into a byte array does a
        # 2-byte word store and clobbers the adjacent byte / next global.
        self.create_instruction("store_ptr", arg1=val_temp, arg2=addr_temp,
                                 extra=node.elem_type)

    # ── M2: Pointers ─────────────────────────────────────────────────────────
    def visit_AddrOfNode(self, node):
        """&x — address of a named variable."""
        # Reconstruct the Var the same way it was created during DefinerNode emit.
        var_var = Var(node.name, type=node.var_type,
                      storage=node.var_storage, scope_id=node.var_scope_id)
        result_temp = self.new_temp("int")
        self.create_instruction("addrof", arg1=var_var, result=result_temp)
        return result_temp

    def visit_DerefNode(self, node):
        """*ptr — load from pointer (rvalue)."""
        ptr_temp = self.generate(node.inner)
        result_temp = self.new_temp(node.type)
        self.create_instruction("load_ptr", arg1=ptr_temp, result=result_temp)
        return result_temp

    def visit_DerefAssignNode(self, node):
        """*ptr = val — store through pointer (statement)."""
        ptr_temp = self.generate(node.ptr_expr)
        val_temp = self.generate(node.value)
        self.create_instruction("store_ptr", arg1=val_temp, arg2=ptr_temp)
