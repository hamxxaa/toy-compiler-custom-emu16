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

        declarations = node.scope.statements

        for declaration in declarations:
            if type(declaration).__name__ == "FunctionDefNode":
                self._register_function(declaration)

        self._validate_main_signature()

        for declaration in declarations:
            decl_name = type(declaration).__name__
            if decl_name in ("DefinerNode", "ArrayDefinerNode"):
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

    def visit_TermNode(self, node):
        left_type = self.visit(node.left)
        right_type = self.visit(node.right)

        if node.operator in ("*", "/"):
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
        self.visit(node.scope)

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
        # All arrays live in the global data section regardless of declaration scope.
        # This gives them static storage (like C's static locals) — safe for non-recursive code.
        arr_type = node.elem_type + "[]"
        self.current_scope.define(node.name, arr_type)
        node.storage = "global"         # always data-section
        node.scope_id = self.current_scope.scope_id

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
