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


class ConstDefNode:
    """const NAME = <const-expr>;  — compile-time integer constant. Resolved to a literal by the
    semantic analyzer (phase 0) and then stripped, so codegen never sees it."""
    def __init__(self, name, value):
        self.name = name
        self.value = value   # expression node, evaluated at compile time


class IfNode:
    def __init__(self, condition, scope, else_body=None):
        self.condition = condition
        self.scope = scope
        self.else_body = else_body


class WhileNode:
    def __init__(self, condition, scope):
        self.condition = condition
        self.scope = scope


class ForNode:
    """for ( <init> ; <condition> ; <post> ) <scope>  — init/post may be None (empty clauses)."""
    def __init__(self, init, condition, post, scope):
        self.init = init
        self.condition = condition
        self.post = post
        self.scope = scope


class BreakNode:
    """break;  — exit the nearest enclosing loop."""
    def __init__(self):
        self.type = None


class ContinueNode:
    """continue;  — jump to the nearest enclosing loop's next iteration (the for-post / while-cond)."""
    def __init__(self):
        self.type = None


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


class NegNode:
    """-expr  — unary minus (lowered to 0 - expr in TAC)."""
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


# ── D6: Structs (data-only, byte-packed; member access via `.`) ───────────────

class StructDefNode:
    """struct Name { <type> <field>; ... }  — collected + stripped by the analyzer (phase 0)."""
    def __init__(self, name, fields):
        self.name = name
        self.fields = fields   # list of (field_type, field_name)


class StructVarNode:
    """var <Struct> name;   or   var <Struct> name[N];  — reserves sizeof(Struct)*count bytes."""
    def __init__(self, name, struct_name, count, is_array):
        self.name = name
        self.struct_name = struct_name
        self.count = count          # int element count, or a const-name str (resolved by analyzer)
        self.is_array = is_array
        self.storage = None
        self.scope_id = None
        self.size_bytes = None      # sizeof(struct) * count, set by analyzer


class MemberAccessNode:
    """rvalue:  v.field   or   arr[i].field"""
    def __init__(self, name, index, field):
        self.name = name
        self.index = index          # None (scalar struct) or an expression (array element)
        self.field = field
        self.type = None
        # resolved by the analyzer:
        self.struct_name = None
        self.field_offset = None
        self.field_type = None
        self.stride = None
        self.storage = None
        self.scope_id = None
        self.decl_type = None       # the declared Var type, for addrof identity


class MemberAssignNode:
    """lvalue:  v.field = expr;   or   arr[i].field = expr;"""
    def __init__(self, name, index, field, value):
        self.name = name
        self.index = index
        self.field = field
        self.value = value
        self.struct_name = None
        self.field_offset = None
        self.field_type = None
        self.stride = None
        self.storage = None
        self.scope_id = None
        self.decl_type = None


# ── Classes (compile-time objects: clone + name-mangle; see class_expander) ────

class ClassDefNode:
    """class Name { <field decls> <method defs> }  — a template; collected + stripped by the expander."""
    def __init__(self, name, fields, methods):
        self.name = name
        self.fields = fields      # DefinerNode / ArrayDefinerNode / StructVarNode (field declarations)
        self.methods = methods    # FunctionDefNode list


class NewInstanceNode:
    """new Class inst;  — stamp out a uniquely-named copy of the class's fields + methods."""
    def __init__(self, class_name, inst_name):
        self.class_name = class_name
        self.inst_name = inst_name


class MethodCallNode:
    """obj.method(args)  or  self.sub.method(args)  — rewritten to a mangled function call."""
    def __init__(self, root, index, parts, args):
        self.root = root          # base identifier or 'self'
        self.index = index        # optional [index] on the root (usually None)
        self.parts = parts        # ['method'] or ['anim','update'] ...
        self.args = args
        self.type = None


class ChainAccessNode:
    """rvalue:  obj.a.b  (2+ levels) — rewritten to a mangled global read."""
    def __init__(self, root, index, parts):
        self.root = root
        self.index = index
        self.parts = parts
        self.type = None


class ChainAssignNode:
    """lvalue:  obj.a.b = value;  (2+ levels) — rewritten to a mangled global write."""
    def __init__(self, root, index, parts, value):
        self.root = root
        self.index = index
        self.parts = parts
        self.value = value
