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
# <if_structure> ::= "if" <condition> "do" <scope>
# <while_structure> ::= "while" <condition> "do" <scope>
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
# <logical_operator> ::= "&" | "|"
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
    IfNode,
    WhileNode,
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
)

conditional_operators = {"<", ">", "==", "<=", ">=", "!="}
logical_operators = {"&", "|"}


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

    def __init__(self, tokens):
        self.tokens = TokenHelper(tokens)

    def parse_program(self):
        # <program> ::= "{" <declaration>* "}"
        self.tokens.consume("{", "SYMBOL")
        declarations = []
        while self.tokens.peek() and self.tokens.peek()[1] != "}":
            declarations.append(self.parse_declaration())
        self.tokens.consume("}", "SYMBOL")
        return ProgramNode(ScopeNode(declarations))

    def parse_declaration(self):
        # <declaration> ::= <global_definer> | <function_def>
        token = self.tokens.peek()
        if token[0] == "TYPE":
            return self.parse_function_def()
        if token[1] == "var":
            return self.parse_definer()
        raise SyntaxError(
            f"Error, expected a declaration but found '{token[1]}' at row {token[2]}, column {token[3]}"
        )

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
            return self.parse_equalize()

    def parse_definer(self):
        # <definer>::= ( "var" <type> <var> ";" )
        #            | ( "var" <type> <var> "=" <expression> ";" )
        #            | ( "var" <type> <var> "[" <number> "]" ";" )   # array
        self.tokens.consume("var", "KEYWORD")
        var_type = self.tokens.consume(expected_type="TYPE")[1]
        var_name = self.tokens.consume(expected_type="IDENTIFIER")[1]
        if self.tokens.peek() and self.tokens.peek()[1] == "[":
            # Array declaration: var type name[N];
            self.tokens.consume("[", "SYMBOL")
            size_tok = self.tokens.consume(expected_type="NUMBER")
            size = int(size_tok[1], 0)  # int(..., 0) handles 0x hex
            self.tokens.consume("]", "SYMBOL")
            self.tokens.consume(";", "SYMBOL")
            return ArrayDefinerNode(var_name, var_type, size)
        value = None
        if self.tokens.peek() and self.tokens.peek()[1] == "=":
            self.tokens.consume("=", "SYMBOL")
            value = self.parse_expression()
        self.tokens.consume(";", "SYMBOL")
        return DefinerNode(var_name, value, var_type)

    def parse_equalize(self):
        # <equalize> ::= <var> "=" <expression> ";"
        #              | <var> "[" <expression> "]" "=" <expression> ";"   (array write)
        var_name = self.tokens.consume(expected_type="IDENTIFIER")[1]
        if self.tokens.peek() and self.tokens.peek()[1] == "[":
            # Array element assignment: arr[i] = expr;
            self.tokens.consume("[", "SYMBOL")
            index = self.parse_expression()
            self.tokens.consume("]", "SYMBOL")
            self.tokens.consume("=", "SYMBOL")
            value = self.parse_expression()
            self.tokens.consume(";", "SYMBOL")
            return IndexAssignNode(var_name, index, value)
        self.tokens.consume("=", "SYMBOL")
        value = self.parse_expression()
        self.tokens.consume(";", "SYMBOL")
        return EqualizeNode(var_name, value)

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
        # <if_structure> ::= "if" <condition> "do" <scope>
        self.tokens.consume("if", "KEYWORD")
        condition = self.parse_condition()
        self.tokens.consume("do", "KEYWORD")
        scope = self.parse_scope()
        return IfNode(condition, scope)

    def parse_while_structure(self):
        # <while_structure> ::= "while" <condition> "do" <scope>
        self.tokens.consume("while", "KEYWORD")
        condition = self.parse_condition()
        self.tokens.consume("do", "KEYWORD")
        scope = self.parse_scope()
        return WhileNode(condition, scope)

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
        # <expression> ::= <term> (("+" | "-") <term>)*
        node = self.parse_term()
        while self.tokens.peek() and self.tokens.peek()[1] in ("+", "-"):
            operator = self.tokens.consume(expected_type="OPERATOR")[1]
            right = self.parse_term()
            node = ExpressionNode(node, operator, right)
        return node

    def parse_term(self):
        # <term> ::= <factor> (("*" | "/") <factor>)*
        node = self.parse_factor()
        while self.tokens.peek() and self.tokens.peek()[1] in ("*", "/"):
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
        elif token[0] == "LOGICAL_OPERATOR" and token[1] == "&":
            # Address-of:  &varname
            self.tokens.consume(expected_type="LOGICAL_OPERATOR")
            var_name = self.tokens.consume(expected_type="IDENTIFIER")[1]
            return AddrOfNode(var_name)
        elif token[0] == "OPERATOR" and token[1] == "*":
            # Pointer dereference as rvalue:  *ptr
            self.tokens.consume(expected_type="OPERATOR")
            inner = self.parse_factor()
            return DerefNode(inner)
        elif token[0] == "IDENTIFIER" and self.tokens.peek_next() and self.tokens.peek_next()[1] == "(":
            return self.parse_function_call()
        elif token[0] == "IDENTIFIER":
            var_name = self.tokens.consume(expected_type="IDENTIFIER")[1]
            # Array index:  arr[i]
            if self.tokens.peek() and self.tokens.peek()[1] == "[":
                self.tokens.consume("[", "SYMBOL")
                index = self.parse_expression()
                self.tokens.consume("]", "SYMBOL")
                return IndexNode(var_name, index)
            return FactorNode(var_name, is_variable=True)
        elif token[0] in ("NUMBER", "SIGNED_NUMBER"):
            number = self.tokens.consume(expected_type=token[0])[1]
            return FactorNode(number, is_variable=False)
        elif token[0] == "BOOLEAN":
            boolean = self.tokens.consume(expected_type="BOOLEAN")[1]
            return FactorNode(boolean, is_variable=False)
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
