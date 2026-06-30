class SymbolTable:
    _scope_counter = 0 
    
    def __init__(self, parent=None):
        self.symbols = {}
        self.parent = parent
        self.storage = "global" if parent is None else "local"
        self.scope_id = SymbolTable._scope_counter
        SymbolTable._scope_counter += 1

    def define(self, name, type):
        if name in self.symbols:
            raise Exception(f"Semantic Error: Variable '{name}' already defined.")
        self.symbols[name] = type

    def lookup(self, name):
        symbol = self.symbols.get(name)
        if symbol:
            return symbol, self.storage, self.scope_id
        if self.parent:
            return self.parent.lookup(name)
        return None, None, None


class SemanticAnalyzer:
    def __init__(self):
        self.current_scope = None
        self.current_function = None
        self.functions = {}
        # Set of (var_name, scope_id) for variables whose address is taken (&x).
        # The allocator uses this to force them onto the stack so &x is valid.
        self.address_taken = set()
        # const NAME -> folded int value (resolved in phase 0, then stripped before codegen).
        self.consts = {}
        # depth of enclosing while/for loops, for break/continue validation.
        self.loop_depth = 0
        # struct Name -> {"fields": {fname: (ftype, offset)}, "order": [...], "size": bytes}
        self.structs = {}

    def analyze(self, ast):
        self.visit(ast)

    def visit(self, node):
        method_name = f"visit_{type(node).__name__}"
        visitor = getattr(self, method_name, self.generic_visit)
        return visitor(node)

    def generic_visit(self, node):
        raise Exception(f"No visit_{type(node).__name__} method")

    def visit_ProgramNode(self, node):
        # Program is analyzed in three phases:
        # 1) collect function signatures, 2) analyze global variables,
        # 3) analyze function bodies.
        self.current_scope = SymbolTable(parent=None)

        # Phase 0: fold compile-time constants + collect struct layouts, then strip both.
        self._collect_consts(node)
        self._collect_structs(node)

        declarations = node.scope.statements

        for declaration in declarations:
            if type(declaration).__name__ == "FunctionDefNode":
                self._register_function(declaration)

        self._validate_main_signature()

        for declaration in declarations:
            decl_name = type(declaration).__name__
            if decl_name in ("DefinerNode", "ArrayDefinerNode", "StructVarNode"):
                self.visit(declaration)
            elif decl_name != "FunctionDefNode":
                raise Exception(
                    f"Semantic Error: Top-level declaration '{decl_name}' is not allowed."
                )

        for declaration in declarations:
            if type(declaration).__name__ == "FunctionDefNode":
                self.visit(declaration)

    def visit_ScopeNode(self, node):
        parent_scope = self.current_scope
        self.current_scope = SymbolTable(parent=parent_scope)
        for statement in node.statements:
            if type(statement).__name__ == "FunctionDefNode":
                raise Exception(
                    "Semantic Error: Nested function definitions are not allowed."
                )
            self.visit(statement)
        self.current_scope = parent_scope

    def visit_FunctionDefNode(self, node):
        if self.current_function is not None:
            raise Exception("Semantic Error: Nested function definitions are not allowed.")

        parent_scope = self.current_scope
        self.current_function = node
        self.current_scope = SymbolTable(parent=parent_scope)

        for param in node.params:
            if param.type == "void":
                raise Exception(
                    f"Type Error: Parameter '{param.name}' in function '{node.name}' cannot have type 'void'."
                )
            self.current_scope.define(param.name, param.type)

        self.visit(node.scope)

        self.current_scope = parent_scope
        self.current_function = None

    def visit_FunctionCallNode(self, node):
        function_info = self.functions.get(node.name)
        if function_info is None:
            raise Exception(f"Semantic Error: Function '{node.name}' is not defined.")

        expected_types = function_info["param_types"]
        if len(node.args) != len(expected_types):
            raise Exception(
                f"Semantic Error: Function '{node.name}' expects {len(expected_types)} argument(s), got {len(node.args)}."
            )

        for idx, arg in enumerate(node.args):
            arg_type = self.visit(arg)
            if not self._assignable(expected_types[idx], arg_type):
                raise Exception(
                    f"Type Error: Function '{node.name}' argument {idx + 1} expects '{expected_types[idx]}', got '{arg_type}'."
                )

        node.type = function_info["return_type"]
        return node.type

    def visit_ReturnNode(self, node):
        if self.current_function is None:
            raise Exception("Semantic Error: 'return' cannot be used outside of a function.")

        function_return_type = self.current_function.return_type
        if function_return_type == "void":
            if node.expression is not None:
                raise Exception(
                    f"Type Error: Function '{self.current_function.name}' returns 'void' and cannot return a value."
                )
            node.type = "void"
            return "void"

        if node.expression is None:
            raise Exception(
                f"Type Error: Function '{self.current_function.name}' must return a value of type '{function_return_type}'."
            )

        return_type = self.visit(node.expression)
        if not self._assignable(function_return_type, return_type):
            raise Exception(
                f"Type Error: Function '{self.current_function.name}' must return '{function_return_type}', got '{return_type}'."
            )

        node.type = return_type
        return return_type

    def visit_DefinerNode(self, node):
        if node.type == "void":
            raise Exception(
                f"Type Error: Variable '{node.name}' cannot have type 'void'."
            )

        if self.current_scope.parent is None and node.name in self.functions:
            raise Exception(
                f"Semantic Error: Global variable '{node.name}' conflicts with function name '{node.name}'."
            )

        self.current_scope.define(node.name, node.type)
        node.storage = self.current_scope.storage
        node.scope_id = self.current_scope.scope_id

        if node.value:
            if self.current_scope.parent is None and self._contains_function_call(node.value):
                raise Exception(
                    f"Semantic Error: Global initializer for '{node.name}' cannot contain function calls."
                )
            value_type = self.visit(node.value)
            if not self._assignable(node.type, value_type):
                raise Exception(
                    f"Type Error: Cannot assign value of type '{value_type}' to variable '{node.name}' of type '{node.type}'."
                )

    def visit_EqualizeNode(self, node):
        var_type, storage, scope_id = self.current_scope.lookup(node.name)
        node.storage = storage
        node.scope_id = scope_id
        # Carry the variable's DECLARED type so codegen builds a Var identical to every other
        # reference to it. Using the RHS value's type instead (e.g. `int w = byteArr[i]`) would
        # make `w:byte` != `w:int` -- the allocator would split them into two registers and the
        # assigned value would never reach later uses. Var identity must be (name, type, storage,
        # scope), so the type here must be the variable's, not the expression's.
        node.type = var_type
        if var_type is None:
            raise Exception(f"Semantic Error: Variable '{node.name}' not defined.")
        value_type = self.visit(node.value)
        if not self._assignable(var_type, value_type):
            raise Exception(
                f"Type Error: Cannot assign value of type '{value_type}' to variable '{node.name}' of type '{var_type}'."
            )

    def visit_ConditionNode(self, node):
        left_type = self.visit(node.left)
        right_type = self.visit(node.right)

        if node.operator in ("!=", "<", ">", "<=", ">="):
            if not (self._is_numeric(left_type) and self._is_numeric(right_type)):
                raise Exception(
                    f"Type Error: Cannot compare values of type '{left_type}' and '{right_type}'."
                )
            node.type = "bool"
            return "bool"
        elif node.operator in ("&&", "||"):
            if left_type != "bool" or right_type != "bool":
                raise Exception(
                    f"Type Error: Logical operations require boolean operands, got '{left_type}' and '{right_type}'."
                )
            node.type = "bool"
            return "bool"
        elif node.operator in ("=="):
            if not self._assignable(left_type, right_type):
                raise Exception(
                    f"Type Error: Cannot compare values of type '{left_type}' and '{right_type}'."
                )
            node.type = "bool"
            return "bool"
        else:
            raise Exception(f"Unknown operator '{node.operator}' in condition.")

    def visit_ExpressionNode(self, node):
        left_type = self.visit(node.left)
        right_type = self.visit(node.right)

        if node.operator in ("+", "-"):
            if self._is_numeric(left_type) and self._is_numeric(right_type):
                node.type = "int"
                return "int"
            else:
                raise Exception(
                    f"Type Error: Cannot perform '{node.operator}' on types '{left_type}' and '{right_type}'."
                )
        elif node.operator in ("&", "|", "^"):
            if self._is_numeric(left_type) and self._is_numeric(right_type):
                node.type = "int"
                return "int"
            else:
                raise Exception(
                    f"Type Error: Bitwise '{node.operator}' requires int/byte operands, got '{left_type}' and '{right_type}'. Use '&&'/'||' for booleans."
                )
        elif node.operator in ("<<", ">>"):
            if self._is_numeric(left_type) and self._is_numeric(right_type):
                node.type = "int"
                return "int"
            else:
                raise Exception(
                    f"Type Error: Shift '{node.operator}' requires int/byte operands, got '{left_type}' and '{right_type}'."
                )
        else:
            raise Exception(f"Unknown operator '{node.operator}' in expression.")

    def visit_BitNotNode(self, node):
        inner_type = self.visit(node.inner)
        if not self._is_numeric(inner_type):
            raise Exception(
                f"Type Error: Bitwise '~' requires an int/byte operand, got '{inner_type}'."
            )
        node.type = "int"
        return "int"

    def visit_NegNode(self, node):
        inner_type = self.visit(node.inner)
        if not self._is_numeric(inner_type):
            raise Exception(
                f"Type Error: Unary '-' requires an int/byte operand, got '{inner_type}'."
            )
        node.type = "int"
        return "int"

    def visit_TermNode(self, node):
        left_type = self.visit(node.left)
        right_type = self.visit(node.right)

        if node.operator in ("*", "/", "%"):
            if self._is_numeric(left_type) and self._is_numeric(right_type):
                node.type = "int"
                return "int"
            else:
                raise Exception(
                    f"Type Error: Cannot perform '{node.operator}' on types '{left_type}' and '{right_type}'."
                )
        else:
            raise Exception(f"Unknown operator '{node.operator}' in term.")

    def visit_FactorNode(self, node):
        # A bare identifier that names a const folds to its literal value (Piece D1).
        if node.is_variable and node.value in self.consts:
            node.is_variable = False
            node.value = str(self.consts[node.value])
            node.type = "int"
            return "int"
        if node.is_variable:
            var_type, storage, scope_id = self.current_scope.lookup(node.value)
            if var_type is None:
                raise Exception(f"Semantic Error: Variable '{node.value}' not defined.")
            node.type = var_type
            node.storage = storage
            node.scope_id = scope_id
            return var_type
        else:
            if self._is_integer_literal(node.value):
                node.type = "int"
                return "int"
            elif self._is_boolean_literal(node.value):
                node.type = "bool"
                return "bool"
            else:
                raise Exception(
                    f"Type Error: Unknown literal type for value '{node.value}', expected integer or boolean literal."
                )

    def _is_integer_literal(self, value):
        if isinstance(value, str):
            if value.lower().startswith("0x"):
                try:
                    int(value, 16)
                    return True
                except ValueError:
                    return False
            if value.startswith("-"):
                return value[1:].isdigit() and len(value) > 1
            return value.isdigit()
        return isinstance(value, int)

    def _is_boolean_literal(self, value):
        if isinstance(value, str):
            return value.lower() in ("true", "false")
        return isinstance(value, bool)

    def _is_numeric(self, t):
        # In M1, `byte` is an alias for `int` (word-stored); both are numeric.
        return t in ("int", "byte")

    def _assignable(self, target, value):
        # Same type, or numeric<->numeric (int/byte mix, truncates on store).
        return target == value or (self._is_numeric(target) and self._is_numeric(value))

    def visit_PrintNode(self, node):
        expression_type = self.visit(node.expression)
        if expression_type == "void":
            raise Exception("Type Error: Cannot print expression of type 'void'.")
        node.expression.type = expression_type
        return expression_type

    def visit_AsmNode(self, node):
        # Raw inline assembly: no type checking, programmer-managed registers.
        node.type = "void"
        return "void"

    def visit_IfNode(self, node):
        condition_type = self.visit(node.condition)
        if condition_type != "bool":
            raise Exception(
                f"Type Error: If condition must be of type 'bool', got '{condition_type}'."
            )
        self.visit(node.scope)
        if node.else_body is not None:
            self.visit(node.else_body)

    def visit_WhileNode(self, node):
        condition_type = self.visit(node.condition)
        if condition_type != "bool":
            raise Exception(
                f"Type Error: While condition must be of type 'bool', got '{condition_type}'."
            )
        self.loop_depth += 1
        self.visit(node.scope)
        self.loop_depth -= 1

    def visit_ForNode(self, node):
        # init (and any var it declares) live in a fresh scope wrapping the loop body.
        parent_scope = self.current_scope
        self.current_scope = SymbolTable(parent=parent_scope)
        if node.init is not None:
            self.visit(node.init)
        condition_type = self.visit(node.condition)
        if condition_type != "bool":
            raise Exception(
                f"Type Error: For condition must be of type 'bool', got '{condition_type}'."
            )
        if node.post is not None:
            self.visit(node.post)
        self.loop_depth += 1
        self.visit(node.scope)
        self.loop_depth -= 1
        self.current_scope = parent_scope

    def visit_SwitchNode(self, node):
        expr_type = self.visit(node.expr)
        if expr_type not in ("int", "byte"):
            raise Exception(
                f"Type Error: switch expression must be 'int' or 'byte', got '{expr_type}'."
            )
        # Fold each case value to a compile-time int and check uniqueness; analyze each body.
        node.case_values = []
        seen = set()
        for (value_expr, body) in node.cases:
            value = self._eval_const_expr(value_expr, "switch case")
            if value in seen:
                raise Exception(f"Semantic Error: duplicate switch case value '{value}'.")
            seen.add(value)
            node.case_values.append(value)
            self.visit(body)
        if node.default is not None:
            self.visit(node.default)

    def visit_BreakNode(self, node):
        if self.loop_depth == 0:
            raise Exception("Semantic Error: 'break' used outside of a loop.")
        node.type = "void"

    def visit_ContinueNode(self, node):
        if self.loop_depth == 0:
            raise Exception("Semantic Error: 'continue' used outside of a loop.")
        node.type = "void"

    def _register_function(self, node):
        if node.name in self.functions:
            raise Exception(f"Semantic Error: Function '{node.name}' already defined.")

        if self.current_scope.lookup(node.name)[0] is not None:
            raise Exception(
                f"Semantic Error: Function name '{node.name}' conflicts with an existing variable."
            )

        param_types = [param.type for param in node.params]
        self.functions[node.name] = {
            "return_type": node.return_type,
            "param_types": param_types,
            "param_names": [param.name for param in node.params],
        }

    def _validate_main_signature(self):
        main_info = self.functions.get("main")
        if main_info is None:
            raise Exception("Semantic Error: Program entry function 'main' is not defined.")

        if main_info["return_type"] != "int":
            raise Exception("Type Error: Function 'main' must return type 'int'.")

        if len(main_info["param_types"]) != 0:
            raise Exception("Semantic Error: Function 'main' must not take parameters.")

    # ── M2: Arrays ──────────────────────────────────────────────────────────
    def visit_ArrayDefinerNode(self, node):
        if node.elem_type == "void":
            raise Exception(
                f"Type Error: Array '{node.name}' cannot have element type 'void'."
            )
        if node.size <= 0:
            raise Exception(
                f"Semantic Error: Array '{node.name}' size must be positive, got {node.size}."
            )
        # Arrays physically live in the data section (static storage) regardless of declaration
        # scope — the backend addresses them via _ensure_data_address. But the storage FIELD must
        # match how index/assign nodes resolve the array (via scope lookup, which returns the
        # declaring scope's storage), so that def_arr and every read/write build the SAME Var
        # identity → the SAME data address. (Forcing "global" here while reads resolved "local"
        # made an initialized array get baked at one address but read at another.)
        arr_type = node.elem_type + "[]"
        self.current_scope.define(node.name, arr_type)
        node.storage = self.current_scope.storage
        node.scope_id = self.current_scope.scope_id

        # Array literal:  var T name[N] = { c0, c1, ... };  — each element must be a compile-time
        # integer constant; at most N of them (the rest are zero-filled). Evaluated values are
        # stashed on the node for the backend to bake into the ROM image.
        node.init_values = None
        if getattr(node, "initializer", None) is not None:
            if len(node.initializer) > node.size:
                raise Exception(
                    f"Semantic Error: array '{node.name}' has {len(node.initializer)} "
                    f"initializers but size {node.size}."
                )
            lo, hi, mask = ((0, 255, 0xFF) if node.elem_type == "byte"
                            else (-32768, 65535, 0xFFFF))
            values = []
            for elem in node.initializer:
                v = self._eval_const_int(elem, node.name)
                if not (lo <= v <= hi):
                    raise Exception(
                        f"Semantic Error: {node.elem_type} array '{node.name}' initializer "
                        f"{v} out of range {lo}..{hi}."
                    )
                values.append(v & mask)
            node.init_values = values

    def _eval_const_int(self, node, arr_name):
        """Evaluate a compile-time-constant initializer element to an int (literals, consts, and
        constant arithmetic over them)."""
        try:
            return self._eval_const_expr(node, arr_name)
        except Exception:
            raise Exception(
                f"Semantic Error: array '{arr_name}' initializer elements must be constant integer expressions."
            )

    # ── const folding (Piece D1) ─────────────────────────────────────────────
    def _collect_consts(self, program):
        """Fold every `const NAME = expr;` to an int, strip the ConstDefNodes, and resolve any
        const-named array sizes. Consts may live in included libs, so this runs on the merged AST.
        Consts must be defined before use (declaration order)."""
        kept = []
        for decl in program.scope.statements:
            if type(decl).__name__ == "ConstDefNode":
                if decl.name in self.consts:
                    raise Exception(f"Semantic Error: const '{decl.name}' already defined.")
                self.consts[decl.name] = self._eval_const_expr(decl.value, decl.name)
            else:
                kept.append(decl)
        program.scope.statements = kept
        for decl in kept:
            if type(decl).__name__ == "ArrayDefinerNode" and isinstance(decl.size, str):
                if decl.size not in self.consts:
                    raise Exception(
                        f"Semantic Error: array '{decl.name}' size '{decl.size}' is not a known const."
                    )
                decl.size = self.consts[decl.size]

    def _eval_const_expr(self, node, ctx=""):
        nm = type(node).__name__
        if nm == "FactorNode":
            if node.is_variable:
                if node.value in self.consts:
                    return self.consts[node.value]
                raise Exception(f"Semantic Error: const '{ctx}' references unknown name '{node.value}'.")
            t = str(node.value).lower()
            if t == "true":
                return 1
            if t == "false":
                return 0
            return int(node.value, 0)
        if nm == "NegNode":
            return -self._eval_const_expr(node.inner, ctx)
        if nm == "BitNotNode":
            return ~self._eval_const_expr(node.inner, ctx)
        if nm in ("ExpressionNode", "TermNode"):
            a = self._eval_const_expr(node.left, ctx)
            b = self._eval_const_expr(node.right, ctx)
            op = node.operator
            if op == "+":  return a + b
            if op == "-":  return a - b
            if op == "*":  return a * b
            if op == "/":  return self._trunc_div(a, b)
            if op == "%":  return a - self._trunc_div(a, b) * b
            if op == "&":  return a & b
            if op == "|":  return a | b
            if op == "^":  return a ^ b
            if op == "<<": return a << b
            if op == ">>": return a >> b
        raise Exception(f"Semantic Error: const '{ctx}' initializer is not a compile-time constant.")

    @staticmethod
    def _trunc_div(a, b):
        if b == 0:
            raise Exception("Semantic Error: division by zero in a const expression.")
        q = abs(a) // abs(b)
        return -q if (a < 0) != (b < 0) else q

    # ── structs (Piece D6) ───────────────────────────────────────────────────
    def _collect_structs(self, program):
        """Compute byte-packed field offsets + sizeof for each struct, then strip the defs."""
        kept = []
        for decl in program.scope.statements:
            if type(decl).__name__ == "StructDefNode":
                if decl.name in self.structs:
                    raise Exception(f"Semantic Error: struct '{decl.name}' already defined.")
                fields, order, offset = {}, [], 0
                for (ftype, fname) in decl.fields:
                    if ftype not in ("int", "byte", "bool"):
                        raise Exception(
                            f"Semantic Error: struct '{decl.name}' field '{fname}' must be int/byte/bool "
                            f"(got '{ftype}'); nested structs are not supported yet."
                        )
                    if fname in fields:
                        raise Exception(f"Semantic Error: struct '{decl.name}' has duplicate field '{fname}'.")
                    fields[fname] = (ftype, offset)
                    order.append(fname)
                    offset += 1 if ftype == "byte" else 2
                self.structs[decl.name] = {"fields": fields, "order": order, "size": offset}
            else:
                kept.append(decl)
        program.scope.statements = kept

    def visit_StructVarNode(self, node):
        if node.struct_name not in self.structs:
            raise Exception(f"Semantic Error: '{node.struct_name}' is not a defined struct type.")
        count = node.count
        if isinstance(count, str):
            if count not in self.consts:
                raise Exception(f"Semantic Error: struct array '{node.name}' size '{count}' is not a known const.")
            count = self.consts[count]
        if count <= 0:
            raise Exception(f"Semantic Error: struct '{node.name}' count must be positive, got {count}.")
        node.count = count
        node.size_bytes = self.structs[node.struct_name]["size"] * count
        decl_type = node.struct_name + ("[]" if node.is_array else "")
        self.current_scope.define(node.name, decl_type)
        node.storage = self.current_scope.storage
        node.scope_id = self.current_scope.scope_id

    def _resolve_member(self, node):
        var_type, storage, scope_id = self.current_scope.lookup(node.name)
        if var_type is None:
            raise Exception(f"Semantic Error: '{node.name}' is not defined.")
        is_array = var_type.endswith("[]")
        struct_name = var_type[:-2] if is_array else var_type
        if struct_name not in self.structs:
            raise Exception(f"Type Error: '{node.name}' (type '{var_type}') is not a struct.")
        if node.index is not None and not is_array:
            raise Exception(f"Type Error: '{node.name}' is a single struct, not an array.")
        if node.index is None and is_array:
            raise Exception(
                f"Type Error: struct array '{node.name}' needs an index, e.g. {node.name}[i].{node.field}."
            )
        sinfo = self.structs[struct_name]
        if node.field not in sinfo["fields"]:
            raise Exception(f"Type Error: struct '{struct_name}' has no field '{node.field}'.")
        if node.index is not None:
            idx_type = self.visit(node.index)
            if not self._is_numeric(idx_type):
                raise Exception(f"Type Error: struct array index must be numeric, got '{idx_type}'.")
        ftype, foff = sinfo["fields"][node.field]
        node.struct_name = struct_name
        node.field_offset = foff
        node.field_type = ftype
        node.stride = sinfo["size"]
        node.storage = storage
        node.scope_id = scope_id
        node.decl_type = var_type
        return ftype

    def visit_MemberAccessNode(self, node):
        node.type = self._resolve_member(node)
        return node.type

    def visit_MemberAssignNode(self, node):
        ftype = self._resolve_member(node)
        val_type = self.visit(node.value)
        if not self._assignable(ftype, val_type):
            raise Exception(
                f"Type Error: cannot assign '{val_type}' to field '{node.field}' of type '{ftype}'."
            )

    def visit_IndexNode(self, node):
        arr_type, storage, scope_id = self.current_scope.lookup(node.array_name)
        if arr_type is None:
            raise Exception(
                f"Semantic Error: Array '{node.array_name}' is not defined."
            )
        if not arr_type.endswith("[]"):
            raise Exception(
                f"Type Error: '{node.array_name}' is not an array (type is '{arr_type}')."
            )
        elem_type = arr_type[:-2]
        idx_type = self.visit(node.index)
        if not self._is_numeric(idx_type):
            raise Exception(
                f"Type Error: Array index must be numeric, got '{idx_type}'."
            )
        node.type = elem_type
        node.elem_type = elem_type
        node.storage = storage
        node.scope_id = scope_id
        return elem_type

    def visit_IndexAssignNode(self, node):
        arr_type, storage, scope_id = self.current_scope.lookup(node.array_name)
        if arr_type is None:
            raise Exception(
                f"Semantic Error: Array '{node.array_name}' is not defined."
            )
        if not arr_type.endswith("[]"):
            raise Exception(
                f"Type Error: '{node.array_name}' is not an array (type is '{arr_type}')."
            )
        elem_type = arr_type[:-2]
        idx_type = self.visit(node.index)
        val_type = self.visit(node.value)
        if not self._is_numeric(idx_type):
            raise Exception(
                f"Type Error: Array index must be numeric, got '{idx_type}'."
            )
        if not self._assignable(elem_type, val_type):
            raise Exception(
                f"Type Error: Cannot assign '{val_type}' to '{elem_type}[]' array."
            )
        node.elem_type = elem_type
        node.storage = storage
        node.scope_id = scope_id

    # ── M2: Pointers ─────────────────────────────────────────────────────────
    def visit_AddrOfNode(self, node):
        var_type, storage, scope_id = self.current_scope.lookup(node.name)
        if var_type is None:
            raise Exception(
                f"Semantic Error: Variable '{node.name}' is not defined."
            )
        # Mark as address-taken so the allocator forces it to a stack slot.
        self.address_taken.add((node.name, scope_id))
        node.var_type = var_type
        node.var_storage = storage
        node.var_scope_id = scope_id
        node.type = "int"
        return "int"

    def visit_DerefNode(self, node):
        ptr_type = self.visit(node.inner)
        if not self._is_numeric(ptr_type):
            raise Exception(
                f"Type Error: Cannot dereference non-numeric type '{ptr_type}'."
            )
        # Default deref type is 'int'. Use 'byte' explicitly if element is known to be byte.
        node.type = "int"
        return "int"

    def visit_DerefAssignNode(self, node):
        ptr_type = self.visit(node.ptr_expr)
        val_type = self.visit(node.value)
        if not self._is_numeric(ptr_type):
            raise Exception(
                f"Type Error: Cannot dereference non-numeric type '{ptr_type}'."
            )
        # No type mismatch check — programmer controls the width through pointer arithmetic.

    def _contains_function_call(self, node):
        node_name = type(node).__name__
        if node_name == "FunctionCallNode":
            return True

        if node_name in ("ExpressionNode", "TermNode", "ConditionNode"):
            return self._contains_function_call(node.left) or self._contains_function_call(node.right)

        return False
