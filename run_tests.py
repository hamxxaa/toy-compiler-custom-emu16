#!/usr/bin/env python3
"""Regression test runner for the toy compiler + desktop emulator."""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
EMU = os.path.join(ROOT, "pc_emu.exe")
ROM_DIR = os.path.join(ROOT, "build", "roms")

TESTS = [
    # (source_file, rom_name, expected_return)
    # ── M1 regression ──────────────────────────────────────────────────────
    ("tests/test_mul.txt",       "test_mul",       42),
    ("tests/test_div.txt",       "test_div",       6),
    ("tests/test_byte.txt",      "test_byte",      300),
    ("tests/test_asm.txt",       "test_asm",       42),
    ("tests/test_asm_loop.txt",  "test_asm_loop",  15),
    ("tests/test_include.txt",   "test_include",   42),
    ("tests/test_plot.txt",      "test_plot",      99),
    ("tests/test_fill.txt",      "test_fill",      7),
    ("tests/test_palette.txt",   "test_palette",   1234),
    ("tests/demo.txt",           "demo",           42),
    # ── M2 new tests ────────────────────────────────────────────────────────
    ("tests/test_hex.txt",       "test_hex",       255),
    ("tests/test_array_int.txt", "test_array_int", 42),
    ("tests/test_array_byte.txt","test_array_byte",7),
    ("tests/test_pointer.txt",   "test_pointer",   99),
    ("tests/test_deref_write.txt","test_deref_write",77),
    # ── M3 bitwise operators ───────────────────────────────────────────────
    ("tests/test_bitand.txt",    "test_bitand",    11),
    ("tests/test_bitor.txt",     "test_bitor",     255),
    ("tests/test_bitxor.txt",    "test_bitxor",    240),
    ("tests/test_bitnot.txt",    "test_bitnot",    255),
    ("tests/test_shift.txt",     "test_shift",     36),
    ("tests/test_logical.txt",   "test_logical",   3),
    # ── M4 else / else-if + drop do ───────────────────────────────────────────
    ("tests/test_else.txt",      "test_else",      2),
    ("tests/test_elseif.txt",    "test_elseif",    30),
    # ── M6 font rendering ─────────────────────────────────────────────────────
    ("tests/test_draw_char.txt",   "test_draw_char",   1),
    ("tests/test_draw_string.txt", "test_draw_string", 1),
    # ── Operand-aware codegen regressions (clobber bug class) ──────────────────
    ("tests/test_global_3param.txt", "test_global_3param", 11),
    ("tests/test_cmp_const_clobber.txt", "test_cmp_const_clobber", 6),
    ("tests/test_rhs_clobber.txt",   "test_rhs_clobber",   30),
    ("tests/test_copy_chain.txt",    "test_copy_chain",    114),
    ("tests/test_ptr_3param.txt",    "test_ptr_3param",    56),
    ("tests/test_addr_param.txt",    "test_addr_param",    42),
    # ── Phase 2: fusion / call-site / DSE ──────────────────────────────────────
    ("tests/test_fusion_loop.txt",   "test_fusion_loop",   10),
    ("tests/test_call_perm.txt",     "test_call_perm",      7),
    ("tests/test_dead_store_alias.txt", "test_dead_store_alias", 7),
    ("tests/test_constprop_order.txt", "test_constprop_order", 7),
    # ── M8: array literals (initialized data baked into the ROM image) ─────────
    ("tests/test_arr_lit_byte.txt",   "test_arr_lit_byte",    3),
    ("tests/test_arr_lit_int.txt",    "test_arr_lit_int",  8738),
    ("tests/test_arr_lit_partial.txt","test_arr_lit_partial", 9),
    ("tests/test_arr_lit_global.txt", "test_arr_lit_global", 13),
    # ── M7: host syscall / interrupt regression (needs --menu so the dev handler is live) ──
    ("tests/test_syscall_echo.txt",   "test_syscall_echo",   42, ["--menu"]),
]


def compile_test(src, rom_name):
    result = subprocess.run(
        [sys.executable, "main.py", src, "--save-rom", rom_name],
        capture_output=True, text=True, cwd=ROOT
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Compiler error for {src}:\n{result.stdout}\n{result.stderr}"
        )


def run_test(rom_name, emu_args=()):
    """Run a ROM and return (return_value, instr_count_or_None, rom_size_bytes)."""
    rom_path = os.path.join(ROM_DIR, rom_name + ".rom")
    result = subprocess.run(
        [EMU, "--rom", rom_path, "--frames", "1", *emu_args],
        capture_output=True, text=True, cwd=ROOT
    )
    rom_bytes = os.path.getsize(rom_path) if os.path.exists(rom_path) else 0
    for line in result.stdout.splitlines():
        if line.startswith("RESULT "):
            got = None
            instr = None
            for token in line.split():
                if token.startswith("return="):
                    got = int(token.split("=")[1])
                elif token.startswith("instr="):
                    instr = int(token.split("=")[1])
            if got is not None:
                return got, instr, rom_bytes
    raise RuntimeError(
        f"No RESULT line from emulator for {rom_name}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


def main():
    print(f"{'Test':<22} {'expect':>8}  {'got':>8}  {'bytes':>6}  {'instr':>8}  {'':>6}")
    print("-" * 66)
    all_pass = True
    total_bytes = 0
    total_instr = 0
    for entry in TESTS:
        src, rom_name, expected = entry[0], entry[1], entry[2]
        emu_args = entry[3] if len(entry) > 3 else []
        try:
            compile_test(src, rom_name)
            got, instr, rom_bytes = run_test(rom_name, emu_args)
            status = "PASS" if got == expected else "FAIL"
            if got != expected:
                all_pass = False
            total_bytes += rom_bytes
            total_instr += (instr or 0)
        except Exception as exc:
            all_pass = False
            print(f"{rom_name:<22} {expected:>8}  {'ERROR':>8}  {'':>6}  {'':>8}  FAIL")
            print(f"  {exc}")
            continue
        instr_str = str(instr) if instr is not None else "n/a"
        print(f"{rom_name:<22} {expected:>8}  {got:>8}  {rom_bytes:>6}  {instr_str:>8}  {status}")
    print("-" * 66)
    print(f"{'TOTAL':<22} {'':>8}  {'':>8}  {total_bytes:>6}  {total_instr:>8}")
    print(f"ALL PASS: {all_pass}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
