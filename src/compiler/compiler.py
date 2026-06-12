import argparse
import sys
import os
import re
from lexer import Tokenizer
from parser import Parser
from parser.parserNodes import ProgramNode, ScopeNode, DefinerNode, FactorNode
from codegen import TAC, TACGenerator
from optimization import Optimizer
from backend import EmuBackend
from analyzer import SemanticAnalyzer

# `include "path";` directive (stripped before tokenizing; handled at the AST level).
INCLUDE_RE = re.compile(r'include\s+"([^"]+)"\s*;')

# Hardware addresses injected as global constants ("prelude") so games and the IO
# library can reference them by name. Matches definitions.h.
HW_CONSTANTS = {
    "VRAM_START": 0xB000,
    "PRAM": 0xAE00,
    "INPUT_ADDR": 0xADFF,
    "SCREEN_WIDTH": 160,
    "SCREEN_HEIGHT": 128,
}


def _hw_constant_nodes():
    nodes = []
    for name, value in HW_CONSTANTS.items():
        nodes.append(DefinerNode(name, FactorNode(str(value), is_variable=False), "int"))
    return nodes


def _collect_unit(path, seen, units):
    """Recursively parse `path` and its includes; append parsed ProgramNodes
    (dependencies first, this file last) to `units`."""
    real = os.path.abspath(path)
    if real in seen:
        return
    seen.add(real)
    try:
        with open(path, "r") as f:
            src = f.read()
    except FileNotFoundError:
        print(f"Error: included file '{path}' not found.")
        sys.exit(1)

    base = os.path.dirname(os.path.abspath(path))
    for inc in INCLUDE_RE.findall(src):
        _collect_unit(os.path.join(base, inc), seen, units)

    stripped = INCLUDE_RE.sub("", src)
    units.append(Parser(tokenize(stripped)).parse_program())


def build_merged_program(input_file):
    """Parse the main file + all includes and splice them into one ProgramNode,
    prefixed with the hardware-constant prelude. Errors on duplicate symbols."""
    seen = set()
    units = []
    _collect_unit(input_file, seen, units)

    merged = []
    names = set()
    for node in _hw_constant_nodes() + [d for u in units for d in u.scope.statements]:
        name = getattr(node, "name", None)
        if name is not None:
            if name in names:
                raise Exception(f"Link Error: duplicate symbol '{name}' across included units.")
            names.add(name)
        merged.append(node)

    return ProgramNode(ScopeNode(merged))


def create_tokenizer():
    tokenizer = Tokenizer()

    tokenizer.add_skip_pattern("( |\t|\n)+")

    tokenizer.add_pattern("KEYWORD", "(while|print|var|if|else|return)", priority=5)
    tokenizer.add_pattern("TYPE", "(int|bool|void|byte)", priority=5)
    tokenizer.add_pattern("BOOLEAN", "(true|false)", priority=6)  # Add boolean literals
    tokenizer.add_pattern(
        "IDENTIFIER", "([a-z]|[A-Z])([a-z]|[A-Z]|[0-9]|_)*", priority=4
    )
    tokenizer.add_pattern("SIGNED_NUMBER", "-[0-9]+", priority=6)
    tokenizer.add_pattern("NUMBER", "(0x[0-9a-fA-F]+|[0-9]+)", priority=3)
    tokenizer.add_pattern("SYMBOL", "(;|,|\\(|\\)|=|}|{|\\[|\\])", priority=2)
    tokenizer.add_pattern("OPERATOR", "(\\+|-|\\*|/)", priority=1)
    tokenizer.add_pattern("CONDITIONAL_OPERATOR", "(<|>|==|<=|>=|!=)", priority=1)
    tokenizer.add_pattern("LOGICAL_OPERATOR", "(&&|\\|\\|)", priority=1)
    tokenizer.add_pattern("BITWISE_OPERATOR", "(&|\\||^|~)", priority=1)
    tokenizer.add_pattern("SHIFT_OPERATOR", "(<<|>>)", priority=2)

    return tokenizer


def tokenize(input_str):
    tokenizer = create_tokenizer()
    tokens = tokenizer.tokenize(input_str)
    return tokens


def compile_program(
    input_file,
    optimize=True,
    output_file="program",
    print_tokens=False,
    print_ast=False,
    print_tac=False,
    print_optimized_tac=False,
    print_alloc=False,
    print_emit=False,
    save_rom=None,
):
    input_str = ""
    try:
        with open(input_file, "r") as raw_code:
            input_str = raw_code.read()
    except FileNotFoundError:
        print(f"Error: Input file '{input_file}' not found.")
        sys.exit(1)
    except PermissionError:
        print(f"Error: Permission denied when reading '{input_file}'.")
        sys.exit(1)
    if not input_str.strip():
        print("Error: Input file is empty.")
        sys.exit(1)

    parser = Parser([])  # used only for print_ast
    if print_tokens:
        stripped = INCLUDE_RE.sub("", input_str)
        for token in tokenize(stripped):
            print(token)
    ast = build_merged_program(input_file)
    sa = SemanticAnalyzer()
    sa.analyze(ast)
    tacg = TACGenerator()
    tac = tacg.generate_tac(ast)
    if print_ast:
        parser.print_ast(ast)
    if print_tac:
        for instr in tac.instructions:
            print(instr)
    if optimize:
        op = Optimizer()
        op.optimize(tac)
        if print_optimized_tac:
            print("After optimization:")
            for instr in tac.instructions:
                print(instr)

    backend = EmuBackend(tac, address_taken=sa.address_taken,
                         print_alloc=print_alloc, print_emit=print_emit)
    rom_bytes = backend.generate()

    # Determine file paths
    build_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "build")
    rom_dir = os.path.join(build_dir, "roms")

    # Ensure directories exist
    os.makedirs(rom_dir, exist_ok=True)

    if save_rom:
        rom_file = os.path.join(rom_dir, save_rom)
        if not rom_file.endswith(".rom"):
            rom_file += ".rom"
    else:
        rom_file = os.path.join(rom_dir, output_file + ".rom")

    with open(rom_file, "wb") as f:
        f.write(rom_bytes)

    print(f"ROM generated: {rom_file}")


def main():
    parser = argparse.ArgumentParser(description="Toy Compiler")
    parser.add_argument("input_file", help="Input source file to compile")
    parser.add_argument(
        "-o",
        "--output",
        default="program",
        help="Output executable name (default: program)",
    )
    parser.add_argument(
        "--no-optimize", action="store_true", help="Disable optimization"
    )
    parser.add_argument("--print-tokens", action="store_true", help="Print tokens")
    parser.add_argument("--print-ast", action="store_true", help="Print AST")
    parser.add_argument("--print-tac", action="store_true", help="Print TAC")
    parser.add_argument(
        "--print-optimized-tac", action="store_true", help="Print optimized TAC"
    )
    parser.add_argument("--print-alloc", action="store_true",
                        help="Print register allocation decisions")
    parser.add_argument("--print-emit", action="store_true",
                        help="Print raw instruction emission trace (address, opcode, operands)")
    parser.add_argument(
        "--save-rom",
        help="Save ROM file with specified name (default: <output>.rom)",
    )

    args = parser.parse_args()

    compile_program(
        input_file=args.input_file,
        optimize=not args.no_optimize,
        output_file=args.output,
        print_tokens=args.print_tokens,
        print_ast=args.print_ast,
        print_tac=args.print_tac,
        print_optimized_tac=args.print_optimized_tac,
        print_alloc=args.print_alloc,
        print_emit=args.print_emit,
        save_rom=args.save_rom,
    )


if __name__ == "__main__":
    main()
