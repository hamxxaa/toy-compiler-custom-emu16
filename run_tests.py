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
    ("examples/test_mul.txt",       "test_mul",       42),
    ("examples/test_div.txt",       "test_div",       6),
    ("examples/test_byte.txt",      "test_byte",      300),
    ("examples/test_asm.txt",       "test_asm",       42),
    ("examples/test_asm_loop.txt",  "test_asm_loop",  15),
    ("examples/test_include.txt",   "test_include",   42),
    ("examples/test_plot.txt",      "test_plot",      99),
    ("examples/test_fill.txt",      "test_fill",      7),
    ("examples/test_palette.txt",   "test_palette",   1234),
    ("examples/demo.txt",           "demo",           42),
    # ── M2 new tests ────────────────────────────────────────────────────────
    ("examples/test_hex.txt",       "test_hex",       255),
    ("examples/test_array_int.txt", "test_array_int", 42),
    ("examples/test_array_byte.txt","test_array_byte",7),
    ("examples/test_pointer.txt",   "test_pointer",   99),
    ("examples/test_deref_write.txt","test_deref_write",77),
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


def run_test(rom_name):
    rom_path = os.path.join(ROM_DIR, rom_name + ".rom")
    result = subprocess.run(
        [EMU, "--rom", rom_path, "--frames", "1"],
        capture_output=True, text=True, cwd=ROOT
    )
    for line in result.stdout.splitlines():
        if line.startswith("RESULT "):
            for token in line.split():
                if token.startswith("return="):
                    return int(token.split("=")[1])
    raise RuntimeError(
        f"No RESULT line from emulator for {rom_name}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


def main():
    print(f"{'Test':<22} {'expect':>8}  {'got':>8}  {'':>6}")
    print("-" * 50)
    all_pass = True
    for src, rom_name, expected in TESTS:
        try:
            compile_test(src, rom_name)
            got = run_test(rom_name)
            status = "PASS" if got == expected else "FAIL"
            if got != expected:
                all_pass = False
        except Exception as exc:
            got = "ERROR"
            status = "FAIL"
            all_pass = False
            print(f"{rom_name:<22} {expected:>8}  {'ERROR':>8}  {status}")
            print(f"  {exc}")
            continue
        print(f"{rom_name:<22} {expected:>8}  {got:>8}  {status}")
    print("-" * 50)
    print(f"ALL PASS: {all_pass}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
