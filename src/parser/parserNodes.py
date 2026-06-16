class ProgramNode:
    def __init__(self, scope):
        self.scope = scope


class ScopeNode:
    def __init__(self, statements):
        self.statements = statements


class ParamNode:
    def __init__(self, param_type, name):
        self._type = param_type
        self.name = name

    @property
    def param_type(self):
        return self._type

    @param_type.setter
    def param_type(self, value):
        self._type = value

    @property
    def type(self):
        return self._type

    @type.setter
    def type(self, value):
        self._type = value


class FunctionDefNode:
    def __init__(self, return_type, name, params, scope):
        self._type = return_type
        self.name = name
        self.params = params
        self.scope = scope
        self.storage = None
        self.scope_id = None
        self.is_naked = False

    @property
    def return_type(self):
        return self._type

    @return_type.setter
    def return_type(self, value):
        self._type = value

    @property
    def type(self):
        return self._type

    @type.setter
    def type(self, value):
        self._type = value


class FunctionCallNode:
    def __init__(self, name, args):
        self.name = name
        self.args = args
        self.type = None
        self.storage = None
        self.scope_id = None


class ReturnNode:
    def __init__(self, expression=None):
        self.expression = expression
        self.type = None


class DefinerNode:
    def __init__(self, name, value=None, type=None):
        self.name = name
        self.value = value
        self.type = type
        self.storage = None
        self.scope_id = None

class EqualizeNode:
    def __init__(self, name, value):
        self.name = name
        self.value = value
        self.storage = None
        self.scope_id = None


class IfNode:
    def __init__(self, condition, scope, else_body=None):
        self.condition = condition
        self.scope = scope
        self.else_body = else_body


class WhileNode:
    def __init__(self, condition, scope):
        self.condition = condition
        self.scope = scope


class PrintNode:
    def __init__(self, expression):
        self.expression = expression


class ConditionNode:
    def __init__(self, left, operator, right):
        self.left = left
        self.operator = operator
        self.right = right
        self.type = None


class ExpressionNode:
    def __init__(self, left, operator, right):
        self.left = left
        self.operator = operator
        self.right = right
        self.type = None


class TermNode:
    def __init__(self, left, operator, right):
        self.left = left
        self.operator = operator
        self.right = right
        self.type = None


class AsmNode:
    def __init__(self, body):
        # body is the raw text between the `asm { ... }` braces.
        self.lines = [line.strip() for line in body.splitlines() if line.strip()]
        self.type = None

    def __repr__(self):
        return f"AsmNode({len(self.lines)} lines)"


# ── M2: Arrays ──────────────────────────────────────────────────────────────

class ArrayDefinerNode:
    """var int arr[N];  or  var byte arr[N];  or  var byte arr[N] = { c0, c1, ... };"""
    def __init__(self, name, elem_type, size, initializer=None):
        self.name = name
        self.elem_type = elem_type  # "int" or "byte"
        self.size = size            # integer element count
        self.initializer = initializer  # list of constant expr nodes, or None
        self.storage = None         # set by sema ("global" for all arrays)
        self.scope_id = None        # set by sema

    @property
    def type(self):
        return self.elem_type + "[]"


class IndexNode:
    """arr[i]  as an rvalue (read)."""
    def __init__(self, array_name, index):
        self.array_name = array_name
        self.index = index      # expression node
        self.type = None        # element type, set by sema
        self.elem_type = None   # set by sema
        self.storage = None     # set by sema
        self.scope_id = None    # set by sema


class IndexAssignNode:
    """arr[i] = expr;  (write, statement-level)."""
    def __init__(self, array_name, index, value):
        self.array_name = array_name
        self.index = index
        self.value = value
        self.elem_type = None   # set by sema
        self.storage = None     # set by sema
        self.scope_id = None    # set by sema


# ── M2: Pointers ─────────────────────────────────────────────────────────────

class AddrOfNode:
    """&x  — address-of a named variable."""
    def __init__(self, name):
        self.name = name
        self.type = "int"       # pointer = 16-bit word address
        self.var_type = None    # type of the pointed-at variable, set by sema
        self.var_storage = None
        self.var_scope_id = None


class DerefNode:
    """*ptr  — dereference a pointer expression (rvalue)."""
    def __init__(self, inner):
        self.inner = inner      # expression that yields the pointer
        self.type = None        # element type, set by sema


class BitNotNode:
    """~expr  — bitwise NOT (lowered to expr ^ 0xFFFF in TAC)."""
    def __init__(self, inner):
        self.inner = inner
        self.type = None


class DerefAssignNode:
    """*ptr = expr;  — dereference and write (statement-level)."""
    def __init__(self, ptr_expr, value):
        self.ptr_expr = ptr_expr    # expression that yields the pointer
        self.value = value          # expression to store


class FactorNode:
    def __init__(self, value, is_variable):
        self.value = value
        self.is_variable = is_variable
        self.type = None
        self.storage = None
        self.scope_id = None

    def __repr__(self):
        return f"FactorNode(value={self.value}, is_variable={self.is_variable})"
