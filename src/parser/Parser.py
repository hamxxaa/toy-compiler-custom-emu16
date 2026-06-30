# L={
# Program accepts only top-level global declarations and function definitions.
# <program> ::= "{" <declaration>* "}"
# <declaration> ::= <global_definer> | <function_def>
#
# <global_definer> ::= "var" <var_type> <var> ";"
#                   | "var" <var_type> <var> "=" <expression> ";"
#
# <function_def> ::= <ret_type> <var> "(" <param_list_opt> ")" <scope>
# <param_list_opt> ::= epsilon | <param> ("," <param>)*
# <param> ::= <var_type> <var>
#
# <scope> ::= "{" <statement>* "}"
# <statement> ::= <definer> | <equalize> | <if_structure> | <print> | <while_structure>
#               | <return_statement> | <expression_statement> | <scope>
#
# <definer> ::= "var" <var_type> <var> ";"
#             | "var" <var_type> <var> "=" <expression> ";"
# <equalize> ::= <var> "=" <expression> ";"
# <return_statement> ::= "return" <expression_opt> ";"
# <expression_opt> ::= epsilon | <expression>
# <expression_statement> ::= <function_call> ";"
#
# <if_structure> ::= "if" <condition> <scope> ("else" (<if_structure> | <scope>))?
# <while_structure> ::= "while" <condition> <scope>
# <print> ::= "print" "(" <expression> ")" ";"
#
# <condition> ::= <expression>
#               | <expression> <conditional_operator> <expression>
#               | "(" <condition> ")" <logical_operator> "(" <condition> ")"
#
# <expression> ::= <term> (("+" | "-") <term>)*
# <term> ::= <factor> (("*" | "/") <factor>)*
# <factor> ::= <function_call> | <var> | <signed_number> | <boolean> | "(" <expression> ")"
# <function_call> ::= <var> "(" <arg_list_opt> ")"
# <arg_list_opt> ::= epsilon | <expression> ("," <expression>)*
#
# Deterministic parse note:
# If current token is IDENTIFIER and next token is "(", parse as <function_call>.
# Otherwise parse as <var>. This removes ambiguity between variable usage and call.
#
# <operator> ::= "+" | "-" | "*" | "/"
# <conditional_operator> ::= "<" | ">" | "==" | "<=" | ">=" | "!="
# <logical_operator> ::= "&&" | "||"
# <bitwise_operator> ::= "&" | "|" | "^"          (flat level, left-assoc; "~" is unary)
# <shift_operator> ::= "<<" | ">>"
# precedence (loosest..tightest): bitwise < shift < additive < term < factor
# <number> ::= <digit>+
# <signed_number> ::= <number> | "-" <number>
# <digit> ::= 1|2|3|4|5|6|7|8|9|0
# <letter> ::= a|b|c....z|A|B|C....Z
# <var> ::= <letter>+
# <var_type> ::= "int" | "bool"
# <ret_type> ::= "int" | "bool" | "void"
# }

from .parserNodes import (
    ProgramNode,
    ScopeNode,
    ParamNode,
    FunctionDefNode,
    FunctionCallNode,
    ReturnNode,
    DefinerNode,
    EqualizeNode,
    ConstDefNode,
    IfNode,
    WhileNode,
    ForNode,
    SwitchNode,
    BreakNode,
    ContinueNode,
    PrintNode,
    ConditionNode,
    ExpressionNode,
    TermNode,
    FactorNode,
    AsmNode,
    # M2
    ArrayDefinerNode,
    IndexNode,
    IndexAssignNode,
    AddrOfNode,
    DerefNode,
    DerefAssignNode,
    BitNotNode,
    NegNode,
    StructDefNode,
    StructVarNode,
    MemberAccessNode,
    MemberAssignNode,
    ClassDefNode,
    NewInstanceNode,
    MethodCallNode,
    ChainAccessNode,
    ChainAssignNode,
)

conditional_operators = {"<", ">", "==", "<=", ">=", "!="}
logical_operators = {"&&", "||"}
bitwise_operators = {"&", "|", "^"}
shift_operators = {"<<", ">>"}


class TokenHelper:
    def __init__(self, tokens):
        self.tokens = tokens
        self.position = 0

    def peek(self):
        if self.position < len(self.tokens):
            return self.tokens[self.position]
        return None

    def peek_next(self):
        if self.position + 1 < len(self.tokens):
            return self.tokens[self.position + 1]
        return None

    def consume(self, expected_value=None, expected_type=None):
        if self.position >= len(self.tokens):
            raise SyntaxError(f"Error, expected '{expected_value}' but found none")
        token = self.tokens[self.position]
        if expected_value and token[1] != expected_value:
            raise SyntaxError(
                f"Error, expected '{expected_value}' but found '{token[1]}' at row {token[2]}, column {token[3]}"
            )
        if expected_type and token[0] != expected_type:
            raise SyntaxError(
                f"Error, expected {expected_type} but found {token[0]} at row {token[2]}, column {token[3]}"
            )
        self.position += 1
        return token


class Parser:

    _string_counter = 0   # class-level so hoisted "__strN" names are unique across all units

    def __init__(self, tokens):
        self.tokens = TokenHelper(tokens)
        self.string_arrays = []   # anonymous byte arrays hoisted from string literals in this unit

    def parse_program(self):
        # <program> ::= "{" <declaration>* "}"
        self.tokens.consume("{", "SYMBOL")
        declarations = []
        while self.tokens.peek() and self.tokens.peek()[1] != "}":
            declarations.append(self.parse_declaration())
        self.tokens.consume("}", "SYMBOL")
        # String literals seen anywhere in this unit become top-level anonymous byte arrays.
        return ProgramNode(ScopeNode(self.string_arrays + declarations))

    def _hoist_string(self):
        # "..."  ->  a NUL-terminated global `var byte __strN[len+1] = { bytes..., 0 };`, and the
        # expression evaluates to its address (&__strN), reusing array-literal baking + addrof.
        text = self.tokens.consume(expected_type="STRING")[1]
        name = f"__str{Parser._string_counter}"
        Parser._string_counter += 1
        init = [FactorNode(str(ord(c) & 0xFF), is_variable=False) for c in text]
        init.append(FactorNode("0", is_variable=False))   # NUL terminator
        self.string_arrays.append(ArrayDefinerNode(name, "byte", len(text) + 1, init))
        return AddrOfNode(name)

    def parse_declaration(self):
        # <declaration> ::= <global_definer> | <function_def>
        token = self.tokens.peek()
        if token[0] == "TYPE":
            return self.parse_function_def()
        if token[1] == "var":
            return self.parse_definer()
        if token[1] == "const":
            return self.parse_const_def()
        if token[1] == "struct":
            return self.parse_struct_def()
        if token[1] == "class":
            return self.parse_class_def()
        if token[1] == "new":
            return self.parse_new_instance()
        raise SyntaxError(
            f"Error, expected a declaration but found '{token[1]}' at row {token[2]}, column {token[3]}"
        )

    def parse_class_def(self):
        # <class_def> ::= "class" <Name> "{" (<field_decl> | <method_def>)* "}"
        #   field_decl = a `var ...` declaration; method_def = an ordinary function def.
        self.tokens.consume("class", "KEYWORD")
        name = self.tokens.consume(expected_type="IDENTIFIER")[1]
        self.tokens.consume("{", "SYMBOL")
        fields = []
        methods = []
        while self.tokens.peek() and self.tokens.peek()[1] != "}":
            tok = self.tokens.peek()
            if tok[1] == "var":
                fields.append(self.parse_definer())
            elif tok[0] == "TYPE":
                methods.append(self.parse_function_def())
            else:
                raise SyntaxError(
                    f"Error in class '{name}': expected a `var` field or a method, found "
                    f"'{tok[1]}' at row {tok[2]}, column {tok[3]}"
                )
        self.tokens.consume("}", "SYMBOL")
        return ClassDefNode(name, fields, methods)

    def parse_new_instance(self):
        # <new_instance> ::= "new" <Class> <name> ";"
        self.tokens.consume("new", "KEYWORD")
        class_name = self.tokens.consume(expected_type="IDENTIFIER")[1]
        inst_name = self.tokens.consume(expected_type="IDENTIFIER")[1]
        self.tokens.consume(";", "SYMBOL")
        return NewInstanceNode(class_name, inst_name)

    def _parse_args(self):
        # "(" (<expression> ("," <expression>)*)? ")"
        self.tokens.consume("(", "SYMBOL")
        args = []
        if self.tokens.peek() and self.tokens.peek()[1] != ")":
            args = self.parse_arg_list()
        self.tokens.consume(")", "SYMBOL")
        return args

    def _is_member_root(self, tok):
        # A dotted-access / method-call root: an identifier or the `self` keyword.
        return tok is not None and (tok[0] == "IDENTIFIER" or (tok[0] == "KEYWORD" and tok[1] == "self"))

    def parse_struct_def(self):
        # <struct_def> ::= "struct" <Name> "{" (<type> <field> ";")* "}"
        self.tokens.consume("struct", "KEYWORD")
        name = self.tokens.consume(expected_type="IDENTIFIER")[1]
        self.tokens.consume("{", "SYMBOL")
        fields = []
        while self.tokens.peek() and self.tokens.peek()[1] != "}":
            ftype = self.tokens.consume(expected_type="TYPE")[1]
            fname = self.tokens.consume(expected_type="IDENTIFIER")[1]
            self.tokens.consume(";", "SYMBOL")
            fields.append((ftype, fname))
        self.tokens.consume("}", "SYMBOL")
        return StructDefNode(name, fields)

    def parse_const_def(self):
        # <const_def> ::= "const" <var> "=" <expression> ";"   (compile-time integer constant)
        self.tokens.consume("const", "KEYWORD")
        name = self.tokens.consume(expected_type="IDENTIFIER")[1]
        self.tokens.consume("=", "SYMBOL")
        value = self.parse_expression()
        self.tokens.consume(";", "SYMBOL")
        return ConstDefNode(name, value)

    def parse_function_def(self):
        # <function_def> ::= <ret_type> <var> "(" <param_list_opt> ")" <scope>
        return_type = self.tokens.consume(expected_type="TYPE")[1]
        name = self.tokens.consume(expected_type="IDENTIFIER")[1]
        self.tokens.consume("(", "SYMBOL")
        params = []
        if self.tokens.peek() and self.tokens.peek()[1] != ")":
            params = self.parse_param_list()
        self.tokens.consume(")", "SYMBOL")
        scope = self.parse_scope()
        node = FunctionDefNode(return_type, name, params, scope)
        # A function whose entire body is a single asm block is "naked":
        # the backend skips its prologue/epilogue and the asm supplies its own RET.
        node.is_naked = (
            len(scope.statements) == 1 and isinstance(scope.statements[0], AsmNode)
        )
        return node

    def parse_param_list(self):
        # <param_list> ::= <param> ("," <param>)*
        params = [self.parse_param()]
        while self.tokens.peek() and self.tokens.peek()[1] == ",":
            self.tokens.consume(",", "SYMBOL")
            params.append(self.parse_param())
        return params

    def parse_param(self):
        # <param> ::= <var_type> <var>
        param_type = self.tokens.consume(expected_type="TYPE")[1]
        param_name = self.tokens.consume(expected_type="IDENTIFIER")[1]
        return ParamNode(param_type, param_name)

    def parse_scope(self):
        # <scope> ::= "{" <statement>* "}"
        self.tokens.consume("{", "SYMBOL")
        statements = []
        while self.tokens.peek() and self.tokens.peek()[1] != "}":
            statements.append(self.parse_statement())
        self.tokens.consume("}", "SYMBOL")
        return ScopeNode(statements)

    def parse_statement(self):
        # <statement> ::= <definer> | <equalize> | <if_structure> | <print> | <while_structure> | <scope> | <return_statement> | <function_call_stmt>
        token = self.tokens.peek()
        if token[0] == "ASM_BLOCK":
            body = self.tokens.consume(expected_type="ASM_BLOCK")[1]
            return AsmNode(body)
        if token[1] == "var":
            return self.parse_definer()
        elif token[1] == "if":
            return self.parse_if_structure()
        elif token[1] == "while":
            return self.parse_while_structure()
        elif token[1] == "for":
            return self.parse_for_structure()
        elif token[1] == "switch":
            return self.parse_switch_structure()
        elif token[1] == "break":
            self.tokens.consume("break", "KEYWORD")
            self.tokens.consume(";", "SYMBOL")
            return BreakNode()
        elif token[1] == "continue":
            self.tokens.consume("continue", "KEYWORD")
            self.tokens.consume(";", "SYMBOL")
            return ContinueNode()
        elif token[1] == "print":
            return self.parse_print()
        elif token[1] == "return":
            return self.parse_return_statement()
        elif token[1] == "{":
            return self.parse_scope()
        elif token[0] == "IDENTIFIER" and self.tokens.peek_next() and self.tokens.peek_next()[1] == "(":
            return self.parse_function_call_statement()
        elif token[0] == "OPERATOR" and token[1] == "*":
            # Pointer deref assign:  *ptr = expr;
            return self.parse_deref_assign()
        else:
            return self.parse_id_statement()

    def parse_definer(self):
        # <definer>::= ( "var" <type> <var> ";" )
        #            | ( "var" <type> <var> "=" <expression> ";" )
        #            | ( "var" <type> <var> "[" <number> "]" ";" )   # array
        self.tokens.consume("var", "KEYWORD")
        type_tok = self.tokens.peek()
        if type_tok and type_tok[0] == "IDENTIFIER":
            # struct variable:  var <Struct> name;  or  var <Struct> name[N];
            return self._parse_struct_var()
        var_type = self.tokens.consume(expected_type="TYPE")[1]
        var_name = self.tokens.consume(expected_type="IDENTIFIER")[1]
        if self.tokens.peek() and self.tokens.peek()[1] == "[":
            # Array declaration: var type name[N];  or  var type name[N] = { c0, c1, ... };
            self.tokens.consume("[", "SYMBOL")
            size_peek = self.tokens.peek()
            if size_peek and size_peek[0] == "IDENTIFIER":
                # const-named size:  var int a[MAXBALLS];  — resolved to an int by the analyzer.
                size = self.tokens.consume(expected_type="IDENTIFIER")[1]
            else:
                size_tok = self.tokens.consume(expected_type="NUMBER")
                size = int(size_tok[1], 0)  # int(..., 0) handles 0x hex
            self.tokens.consume("]", "SYMBOL")
            initializer = None
            if self.tokens.peek() and self.tokens.peek()[1] == "=":
                self.tokens.consume("=", "SYMBOL")
                self.tokens.consume("{", "SYMBOL")
                initializer = []
                if not (self.tokens.peek() and self.tokens.peek()[1] == "}"):
                    initializer.append(self.parse_expression())
                    while self.tokens.peek() and self.tokens.peek()[1] == ",":
                        self.tokens.consume(",", "SYMBOL")
                        if self.tokens.peek() and self.tokens.peek()[1] == "}":
                            break  # allow a trailing comma
                        initializer.append(self.parse_expression())
                self.tokens.consume("}", "SYMBOL")
            self.tokens.consume(";", "SYMBOL")
            return ArrayDefinerNode(var_name, var_type, size, initializer)
        value = None
        if self.tokens.peek() and self.tokens.peek()[1] == "=":
            self.tokens.consume("=", "SYMBOL")
            value = self.parse_expression()
        self.tokens.consume(";", "SYMBOL")
        return DefinerNode(var_name, value, var_type)

    def _parse_struct_var(self):
        # var <Struct> name;  |  var <Struct> name[N];   (N is a NUMBER or a const name)
        struct_name = self.tokens.consume(expected_type="IDENTIFIER")[1]
        var_name = self.tokens.consume(expected_type="IDENTIFIER")[1]
        is_array = False
        count = 1
        if self.tokens.peek() and self.tokens.peek()[1] == "[":
            is_array = True
            self.tokens.consume("[", "SYMBOL")
            sp = self.tokens.peek()
            if sp and sp[0] == "IDENTIFIER":
                count = self.tokens.consume(expected_type="IDENTIFIER")[1]
            else:
                count = int(self.tokens.consume(expected_type="NUMBER")[1], 0)
            self.tokens.consume("]", "SYMBOL")
        self.tokens.consume(";", "SYMBOL")
        return StructVarNode(var_name, struct_name, count, is_array)

    def parse_equalize(self):
        # <equalize> ::= <var> "=" <expression> ";"
        #              | <var> "[" <expression> "]" "=" <expression> ";"   (array write)
        var_name = self.tokens.consume(expected_type="IDENTIFIER")[1]
        index = None
        if self.tokens.peek() and self.tokens.peek()[1] == "[":
            self.tokens.consume("[", "SYMBOL")
            index = self.parse_expression()
            self.tokens.consume("]", "SYMBOL")
        if self.tokens.peek() and self.tokens.peek()[1] == ".":
            # Struct member assignment:  v.field = expr;  or  arr[i].field = expr;
            self.tokens.consume(".", "SYMBOL")
            field = self.tokens.consume(expected_type="IDENTIFIER")[1]
            self.tokens.consume("=", "SYMBOL")
            value = self.parse_expression()
            self.tokens.consume(";", "SYMBOL")
            return MemberAssignNode(var_name, index, field, value)
        if index is not None:
            # Array element assignment: arr[i] = expr;
            self.tokens.consume("=", "SYMBOL")
            value = self.parse_expression()
            self.tokens.consume(";", "SYMBOL")
            return IndexAssignNode(var_name, index, value)
        self.tokens.consume("=", "SYMBOL")
        value = self.parse_expression()
        self.tokens.consume(";", "SYMBOL")
        return EqualizeNode(var_name, value)

    def parse_id_statement(self):
        # A statement rooted at an identifier or `self`: an assignment (plain / array / member / chain)
        # or a method-call statement.  Subsumes the old parse_equalize and adds class method calls.
        if not self._is_member_root(self.tokens.peek()):
            tok = self.tokens.peek()
            raise SyntaxError(
                f"Error, unexpected '{tok[1]}' at row {tok[2]}, column {tok[3]}"
            )
        root = self.tokens.consume()[1]              # IDENTIFIER or 'self'
        index = None
        if self.tokens.peek() and self.tokens.peek()[1] == "[":
            self.tokens.consume("[", "SYMBOL")
            index = self.parse_expression()
            self.tokens.consume("]", "SYMBOL")
        parts = []
        while self.tokens.peek() and self.tokens.peek()[1] == ".":
            self.tokens.consume(".", "SYMBOL")
            parts.append(self.tokens.consume(expected_type="IDENTIFIER")[1])
        if self.tokens.peek() and self.tokens.peek()[1] == "(":
            # method-call statement:  obj.m(args);  /  self.sub.m(args);
            args = self._parse_args()
            self.tokens.consume(";", "SYMBOL")
            return MethodCallNode(root, index, parts, args)
        # assignment
        self.tokens.consume("=", "SYMBOL")
        value = self.parse_expression()
        self.tokens.consume(";", "SYMBOL")
        if len(parts) >= 2:
            return ChainAssignNode(root, index, parts, value)
        if len(parts) == 1:
            return MemberAssignNode(root, index, parts[0], value)
        if index is not None:
            return IndexAssignNode(root, index, value)
        return EqualizeNode(root, value)

    def parse_deref_assign(self):
        # *ptr = expr;
        self.tokens.consume(expected_type="OPERATOR")   # consume '*'
        ptr_factor = self.parse_factor()                # the pointer variable/expr
        self.tokens.consume("=", "SYMBOL")
        value = self.parse_expression()
        self.tokens.consume(";", "SYMBOL")
        return DerefAssignNode(ptr_factor, value)

    def parse_return_statement(self):
        # <return_statement> ::= "return" <expression_opt> ";"
        self.tokens.consume("return", "KEYWORD")
        expression = None
        if self.tokens.peek() and self.tokens.peek()[1] != ";":
            expression = self.parse_expression()
        self.tokens.consume(";", "SYMBOL")
        return ReturnNode(expression)

    def parse_function_call_statement(self):
        # <function_call_stmt> ::= <function_call> ";"
        call = self.parse_function_call()
        self.tokens.consume(";", "SYMBOL")
        return call

    def parse_function_call(self):
        # <function_call> ::= <var> "(" <arg_list_opt> ")"
        name = self.tokens.consume(expected_type="IDENTIFIER")[1]
        self.tokens.consume("(", "SYMBOL")
        args = []
        if self.tokens.peek() and self.tokens.peek()[1] != ")":
            args = self.parse_arg_list()
        self.tokens.consume(")", "SYMBOL")
        return FunctionCallNode(name, args)

    def parse_arg_list(self):
        # <arg_list> ::= <expression> ("," <expression>)*
        args = [self.parse_expression()]
        while self.tokens.peek() and self.tokens.peek()[1] == ",":
            self.tokens.consume(",", "SYMBOL")
            args.append(self.parse_expression())
        return args

    def parse_if_structure(self):
        # <if_structure> ::= "if" <condition> <scope> ("else" (<if_structure> | <scope>))?
        self.tokens.consume("if", "KEYWORD")
        condition = self.parse_condition()
        scope = self.parse_scope()
        else_body = None
        if self.tokens.peek() and self.tokens.peek()[1] == "else":
            self.tokens.consume("else", "KEYWORD")
            if self.tokens.peek() and self.tokens.peek()[1] == "if":
                else_body = self.parse_if_structure()
            else:
                else_body = self.parse_scope()
        return IfNode(condition, scope, else_body)

    def parse_while_structure(self):
        # <while_structure> ::= "while" <condition> <scope>
        self.tokens.consume("while", "KEYWORD")
        condition = self.parse_condition()
        scope = self.parse_scope()
        return WhileNode(condition, scope)

    def parse_for_structure(self):
        # <for> ::= "for" "(" <init>? ";" <condition> ";" <post>? ")" <scope>
        self.tokens.consume("for", "KEYWORD")
        self.tokens.consume("(", "SYMBOL")
        init = self._parse_for_clause()
        self.tokens.consume(";", "SYMBOL")
        condition = self.parse_condition()
        self.tokens.consume(";", "SYMBOL")
        post = self._parse_for_clause()
        self.tokens.consume(")", "SYMBOL")
        scope = self.parse_scope()
        return ForNode(init, condition, post, scope)

    def _parse_for_clause(self):
        # An init/post clause WITHOUT a trailing ';': a var-definer, an assignment, or empty.
        tok = self.tokens.peek()
        if tok and tok[1] in (";", ")"):
            return None
        if tok[1] == "var":
            self.tokens.consume("var", "KEYWORD")
            var_type = self.tokens.consume(expected_type="TYPE")[1]
            var_name = self.tokens.consume(expected_type="IDENTIFIER")[1]
            value = None
            if self.tokens.peek() and self.tokens.peek()[1] == "=":
                self.tokens.consume("=", "SYMBOL")
                value = self.parse_expression()
            return DefinerNode(var_name, value, var_type)
        var_name = self.tokens.consume(expected_type="IDENTIFIER")[1]
        if self.tokens.peek() and self.tokens.peek()[1] == "[":
            self.tokens.consume("[", "SYMBOL")
            index = self.parse_expression()
            self.tokens.consume("]", "SYMBOL")
            self.tokens.consume("=", "SYMBOL")
            value = self.parse_expression()
            return IndexAssignNode(var_name, index, value)
        self.tokens.consume("=", "SYMBOL")
        value = self.parse_expression()
        return EqualizeNode(var_name, value)

    def parse_switch_structure(self):
        # <switch> ::= "switch" "(" <expression> ")" "{" (<case> | <default>)+ "}"
        # <case>    ::= "case" <const-expr> ":" <statement>*      (auto-break; no fallthrough)
        # <default> ::= "default" ":" <statement>*
        self.tokens.consume("switch", "KEYWORD")
        self.tokens.consume("(", "SYMBOL")
        expr = self.parse_expression()
        self.tokens.consume(")", "SYMBOL")
        self.tokens.consume("{", "SYMBOL")
        cases = []
        default = None
        while self.tokens.peek() and self.tokens.peek()[1] != "}":
            tok = self.tokens.peek()
            if tok[1] == "case":
                self.tokens.consume("case", "KEYWORD")
                value = self.parse_expression()
                self.tokens.consume(":", "SYMBOL")
                cases.append((value, self._parse_case_body()))
            elif tok[1] == "default":
                if default is not None:
                    raise SyntaxError(
                        f"Error: duplicate 'default' in switch at row {tok[2]}, column {tok[3]}"
                    )
                self.tokens.consume("default", "KEYWORD")
                self.tokens.consume(":", "SYMBOL")
                default = self._parse_case_body()
            else:
                raise SyntaxError(
                    f"Error: expected 'case' or 'default' in switch but found '{tok[1]}' "
                    f"at row {tok[2]}, column {tok[3]}"
                )
        self.tokens.consume("}", "SYMBOL")
        if not cases:
            raise SyntaxError("Error: a switch must have at least one 'case'.")
        return SwitchNode(expr, cases, default)

    def _parse_case_body(self):
        # Statements up to the next 'case'/'default'/'}'. Wrapped in a ScopeNode so each case body
        # gets its own variable scope; codegen auto-breaks to the switch end after it.
        statements = []
        while self.tokens.peek() and self.tokens.peek()[1] not in ("case", "default", "}"):
            statements.append(self.parse_statement())
        return ScopeNode(statements)

    def parse_print(self):
        # <print> ::= "print" "(" <expression> ")" ";"
        self.tokens.consume("print", "KEYWORD")
        self.tokens.consume("(", "SYMBOL")
        expression = self.parse_expression()
        self.tokens.consume(")", "SYMBOL")
        self.tokens.consume(";", "SYMBOL")
        return PrintNode(expression)

    def parse_condition(self):
        # <condition> ::= <expression> | <expression> <conditional_operator> <expression> | "(" <condition> ")" <logical_operator> "(" <condition> ")"
        if self.tokens.peek()[1] == "(":
            self.tokens.consume("(", "SYMBOL")
            node = self.parse_condition()
            self.tokens.consume(")", "SYMBOL")
            while self.tokens.peek() and self.tokens.peek()[1] in logical_operators:
                operator = self.tokens.consume(expected_type="LOGICAL_OPERATOR")[1]
                self.tokens.consume("(", "SYMBOL")
                right = self.parse_condition()
                self.tokens.consume(")", "SYMBOL")
                node = ConditionNode(node, operator, right)
            # Allow a comparison after a parenthesized group:  (x & 1) == 1
            if self.tokens.peek() and self.tokens.peek()[1] in conditional_operators:
                operator = self.tokens.consume(expected_type="CONDITIONAL_OPERATOR")[1]
                right = self.parse_expression()
                node = ConditionNode(node, operator, right)
            return node
        else:
            left = self.parse_expression()
            if (
                not self.tokens.peek()
                or self.tokens.peek()[1] not in conditional_operators
            ):
                return left
            operator = self.tokens.consume(expected_type="CONDITIONAL_OPERATOR")[1]
            right = self.parse_expression()
            return ConditionNode(left, operator, right)

    def parse_expression(self):
        # <expression> ::= <shift> (("&" | "|" | "^") <shift>)*   flat, left-assoc
        node = self.parse_shift()
        while self.tokens.peek() and self.tokens.peek()[1] in bitwise_operators:
            operator = self.tokens.consume(expected_type="BITWISE_OPERATOR")[1]
            right = self.parse_shift()
            node = ExpressionNode(node, operator, right)
        return node

    def parse_shift(self):
        # <shift> ::= <additive> (("<<" | ">>") <additive>)*
        node = self.parse_additive()
        while self.tokens.peek() and self.tokens.peek()[1] in shift_operators:
            operator = self.tokens.consume(expected_type="SHIFT_OPERATOR")[1]
            right = self.parse_additive()
            node = ExpressionNode(node, operator, right)
        return node

    def parse_additive(self):
        # <additive> ::= <term> (("+" | "-") <term>)*
        node = self.parse_term()
        while self.tokens.peek() and self.tokens.peek()[1] in ("+", "-"):
            operator = self.tokens.consume(expected_type="OPERATOR")[1]
            right = self.parse_term()
            node = ExpressionNode(node, operator, right)
        return node

    def parse_term(self):
        # <term> ::= <factor> (("*" | "/" | "%") <factor>)*
        node = self.parse_factor()
        while self.tokens.peek() and self.tokens.peek()[1] in ("*", "/", "%"):
            operator = self.tokens.consume(expected_type="OPERATOR")[1]
            right = self.parse_factor()
            node = TermNode(node, operator, right)
        return node

    def parse_factor(self):
        # <factor> ::= <var> | <signed_number> | <boolean> | "(" <expression> ")"
        #            | <function_call> | <var> "[" <expr> "]"
        #            | "&" <var>         (address-of)
        #            | "*" <factor>      (pointer deref as rvalue)
        token = self.tokens.peek()
        if token[1] == "(":
            self.tokens.consume("(", "SYMBOL")
            node = self.parse_expression()
            self.tokens.consume(")", "SYMBOL")
            return node
        elif token[0] == "BITWISE_OPERATOR" and token[1] == "&":
            # Address-of:  &varname
            self.tokens.consume(expected_type="BITWISE_OPERATOR")
            var_name = self.tokens.consume(expected_type="IDENTIFIER")[1]
            return AddrOfNode(var_name)
        elif token[0] == "BITWISE_OPERATOR" and token[1] == "~":
            # Bitwise NOT:  ~expr
            self.tokens.consume(expected_type="BITWISE_OPERATOR")
            inner = self.parse_factor()
            return BitNotNode(inner)
        elif token[0] == "OPERATOR" and token[1] == "*":
            # Pointer dereference as rvalue:  *ptr
            self.tokens.consume(expected_type="OPERATOR")
            inner = self.parse_factor()
            return DerefNode(inner)
        elif token[0] == "OPERATOR" and token[1] == "-":
            # Unary minus:  -expr   (`-5` literals already lex as SIGNED_NUMBER; this is `-x`, `-(e)`, ...)
            self.tokens.consume(expected_type="OPERATOR")
            inner = self.parse_factor()
            return NegNode(inner)
        elif token[0] == "IDENTIFIER" and self.tokens.peek_next() and self.tokens.peek_next()[1] == "(":
            return self.parse_function_call()
        elif self._is_member_root(token):
            root = self.tokens.consume()[1]          # IDENTIFIER or 'self'
            index = None
            if self.tokens.peek() and self.tokens.peek()[1] == "[":      # arr[i]
                self.tokens.consume("[", "SYMBOL")
                index = self.parse_expression()
                self.tokens.consume("]", "SYMBOL")
            parts = []                                                    # .field chain
            while self.tokens.peek() and self.tokens.peek()[1] == ".":
                self.tokens.consume(".", "SYMBOL")
                parts.append(self.tokens.consume(expected_type="IDENTIFIER")[1])
            if self.tokens.peek() and self.tokens.peek()[1] == "(":       # method call rvalue
                return MethodCallNode(root, index, parts, self._parse_args())
            if len(parts) >= 2:                                           # class chain a.b.c
                return ChainAccessNode(root, index, parts)
            if len(parts) == 1:                                           # struct field or class field
                return MemberAccessNode(root, index, parts[0])
            if index is not None:
                return IndexNode(root, index)
            return FactorNode(root, is_variable=True)
        elif token[0] in ("NUMBER", "SIGNED_NUMBER"):
            number = self.tokens.consume(expected_type=token[0])[1]
            return FactorNode(number, is_variable=False)
        elif token[0] == "BOOLEAN":
            boolean = self.tokens.consume(expected_type="BOOLEAN")[1]
            return FactorNode(boolean, is_variable=False)
        elif token[0] == "STRING":
            return self._hoist_string()
        else:
            raise SyntaxError(
                f"Error, expected '(', 'IDENTIFIER', 'NUMBER', 'SIGNED_NUMBER', or 'BOOLEAN' but found '{token[1]}' at row {token[2]}, column {token[3]}"
            )

    def print_ast(self, node, indent=0):
        prefix = "  " * indent

        if isinstance(node, ProgramNode):
            print(f"{prefix}Program:")
            self.print_ast(node.scope, indent + 1)

        if isinstance(node, ScopeNode):
            print(f"{prefix}Scope:")
            for stmt in node.statements:
                self.print_ast(stmt, indent + 1)

        elif isinstance(node, FunctionDefNode):
            print(f"{prefix}FunctionDef: {node.return_type} {node.name}")
            if node.params:
                print(f"{prefix}  Params:")
                for param in node.params:
                    self.print_ast(param, indent + 2)
            print(f"{prefix}  Body:")
            self.print_ast(node.scope, indent + 2)

        elif isinstance(node, ParamNode):
            print(f"{prefix}Param: {node.type} {node.name}")

        elif isinstance(node, FunctionCallNode):
            print(f"{prefix}Call: {node.name}")
            for arg in node.args:
                self.print_ast(arg, indent + 1)

        elif isinstance(node, ReturnNode):
            print(f"{prefix}Return:")
            if node.expression is not None:
                self.print_ast(node.expression, indent + 1)

        elif isinstance(node, DefinerNode):
            print(f"{prefix}Definer: var {node.name}", end="")
            if node.value:
                print(" =")
                self.print_ast(node.value, indent + 1)
            else:
                print()

        elif isinstance(node, EqualizeNode):
            print(f"{prefix}Equalize: {node.name} =")
            self.print_ast(node.value, indent + 1)

        elif isinstance(node, IfNode):
            print(f"{prefix}If:")
            self.print_ast(node.condition, indent + 1)
            self.print_ast(node.scope, indent + 1)
            if node.else_body is not None:
                print(f"{prefix}Else:")
                self.print_ast(node.else_body, indent + 1)

        elif isinstance(node, WhileNode):
            print(f"{prefix}While:")
            self.print_ast(node.condition, indent + 1)
            self.print_ast(node.scope, indent + 1)

        elif isinstance(node, PrintNode):
            print(f"{prefix}Print:")
            self.print_ast(node.expression, indent + 1)

        elif isinstance(node, ConditionNode):
            print(f"{prefix}Condition: {node.operator}")
            self.print_ast(node.left, indent + 1)
            self.print_ast(node.right, indent + 1)

        elif isinstance(node, ExpressionNode):
            print(f"{prefix}Expression: {node.operator}")
            self.print_ast(node.left, indent + 1)
            self.print_ast(node.right, indent + 1)

        elif isinstance(node, TermNode):
            print(f"{prefix}Term: {node.operator}")
            self.print_ast(node.left, indent + 1)
            self.print_ast(node.right, indent + 1)

        elif isinstance(node, AsmNode):
            print(f"{prefix}Asm: {len(node.lines)} line(s)")
            for line in node.lines:
                print(f"{prefix}  | {line}")

        elif isinstance(node, FactorNode):
            if node.is_variable:
                print(f"{prefix}Var: {node.value}")
            else:
                print(f"{prefix}Num: {node.value}")
