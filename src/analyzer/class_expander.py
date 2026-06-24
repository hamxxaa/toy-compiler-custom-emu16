"""Compile-time class expansion (monomorphization + name-mangling).

A `class` is a template; `new Class inst;` stamps out a copy of its fields + methods with a unique
`inst__` prefix, and `obj.method()` / `obj.field` / `self.x` rewrite to those mangled symbols. This runs
on the merged AST *before* the semantic analyzer, so the rest of the compiler only ever sees plain
globals + functions (+ struct nodes) — zero backend changes.

v1 scope: flat classes + composition (a field may be another class instance). A class field must be a
**primitive** (int/byte/bool) or a **single composed class instance** (`var SomeClass sub;`); arrays and
structs as class fields are not supported yet (use a plain global for big buffers). No inheritance,
no runtime-indexed object arrays.
"""
import copy

from parser.parserNodes import FunctionCallNode, FactorNode, EqualizeNode


# node type -> child attributes to recurse into. Tuple ("attr", True) means a list of children.
CHILD_ATTRS = {
    "ScopeNode":        [("statements", True)],
    "FunctionDefNode":  ["scope"],
    "ReturnNode":       ["expression"],
    "DefinerNode":      ["value"],
    "EqualizeNode":     ["value"],
    "IfNode":           ["condition", "scope", "else_body"],
    "WhileNode":        ["condition", "scope"],
    "ForNode":          ["init", "condition", "post", "scope"],
    "PrintNode":        ["expression"],
    "ExpressionNode":   ["left", "right"],
    "TermNode":         ["left", "right"],
    "ConditionNode":    ["left", "right"],
    "NegNode":          ["inner"],
    "BitNotNode":       ["inner"],
    "DerefNode":        ["inner"],
    "DerefAssignNode":  ["ptr_expr", "value"],
    "IndexNode":        ["index"],
    "IndexAssignNode":  ["index", "value"],
    "FunctionCallNode": [("args", True)],
    "ArrayDefinerNode": [("initializer", True)],
}


def expand_classes(program):
    classes = {}            # class name -> ClassDefNode
    instances = []          # (inst_name, class_name) in declaration order
    kept = []
    for s in program.scope.statements:
        t = type(s).__name__
        if t == "ClassDefNode":
            if s.name in classes:
                raise Exception(f"Semantic Error: class '{s.name}' already defined.")
            classes[s.name] = s
        elif t == "NewInstanceNode":
            instances.append((s.inst_name, s.class_name))
        else:
            kept.append(s)

    if not classes and not instances:
        return

    inst_classes = {}       # top-level instance name -> class name
    for (nm, cls) in instances:
        if cls not in classes:
            raise Exception(f"Semantic Error: 'new {cls} {nm}': unknown class '{cls}'.")
        if nm in inst_classes:
            raise Exception(f"Semantic Error: instance '{nm}' already defined.")
        inst_classes[nm] = cls

    rw = _Rewriter(inst_classes)
    new_decls = []
    for (nm, cls) in instances:
        _emit_instance(nm, cls, classes, rw, new_decls)
    for s in kept:                       # rewrite call sites in the game code
        rw.rewrite(s, self_prefix=None)

    program.scope.statements = new_decls + kept


def _emit_instance(prefix, cls_name, classes, rw, out):
    cls = classes[cls_name]
    for field in cls.fields:
        ft = type(field).__name__
        if ft == "DefinerNode":                                   # primitive field
            f = copy.deepcopy(field)
            f.name = prefix + "__" + field.name
            out.append(f)
        elif ft == "StructVarNode" and field.struct_name in classes and not field.is_array:
            _emit_instance(prefix + "__" + field.name, field.struct_name, classes, rw, out)   # composition
        else:
            raise Exception(
                f"Semantic Error: class field '{getattr(field, 'name', '?')}' must be a primitive "
                f"(int/byte/bool) or a single composed class instance; arrays/structs as class fields "
                f"are not supported yet (use a global for big buffers)."
            )
    for method in cls.methods:
        m = copy.deepcopy(method)
        m.name = prefix + "__" + method.name
        rw.rewrite(m.scope, self_prefix=prefix)
        out.append(m)


class _Rewriter:
    def __init__(self, inst_classes):
        self.inst_classes = inst_classes

    def _prefix_for(self, root, self_prefix):
        """Mangling prefix if `root` is `self` or a known instance, else None (not an instance)."""
        if root == "self":
            if self_prefix is None:
                raise Exception("Semantic Error: 'self' used outside a class method.")
            return self_prefix
        if root in self.inst_classes:
            return root
        return None

    def rewrite(self, node, self_prefix):
        """Return the rewritten node (possibly a new node); mutate children in place."""
        if node is None:
            return None
        t = type(node).__name__

        if t == "MethodCallNode":
            args = [self.rewrite(a, self_prefix) for a in node.args]
            p = self._prefix_for(node.root, self_prefix)
            if p is None:
                raise Exception(f"Semantic Error: method call on '{node.root}', which is not a class instance.")
            return FunctionCallNode(p + "__" + "__".join(node.parts), args)
        if t == "ChainAccessNode":
            p = self._prefix_for(node.root, self_prefix)
            if p is None:
                raise Exception(f"Semantic Error: '{node.root}.{'.'.join(node.parts)}' — '{node.root}' is not a class instance.")
            return FactorNode(p + "__" + "__".join(node.parts), is_variable=True)
        if t == "ChainAssignNode":
            value = self.rewrite(node.value, self_prefix)
            p = self._prefix_for(node.root, self_prefix)
            if p is None:
                raise Exception(f"Semantic Error: '{node.root}.{'.'.join(node.parts)}' — '{node.root}' is not a class instance.")
            return EqualizeNode(p + "__" + "__".join(node.parts), value)
        if t == "MemberAccessNode":
            node.index = self.rewrite(node.index, self_prefix)
            p = self._prefix_for(node.name, self_prefix)
            if p is not None:
                return FactorNode(p + "__" + node.field, is_variable=True)
            return node                                            # struct member access — leave it
        if t == "MemberAssignNode":
            node.index = self.rewrite(node.index, self_prefix)
            node.value = self.rewrite(node.value, self_prefix)
            p = self._prefix_for(node.name, self_prefix)
            if p is not None:
                return EqualizeNode(p + "__" + node.field, node.value)
            return node                                            # struct member assign — leave it

        for attr in CHILD_ATTRS.get(t, []):
            if isinstance(attr, tuple):
                name = attr[0]
                lst = getattr(node, name, None)
                if lst is not None:
                    setattr(node, name, [self.rewrite(c, self_prefix) for c in lst])
            else:
                child = getattr(node, attr, None)
                if child is not None:
                    setattr(node, attr, self.rewrite(child, self_prefix))
        return node
