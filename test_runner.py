#!/usr/bin/env python3
"""
Emulator Test Interface
Compile TAC code, generate ROM, and verify behavior.
"""

import os
import sys
import subprocess
import shutil
import re
from pathlib import Path

class EmulatorTestRunner:
    def __init__(self, toy_compiler_path):
        self.toy_compiler_path = Path(toy_compiler_path)
        self.main_script = self.toy_compiler_path / "main.py"
        self.rom_path = self.toy_compiler_path / "build" / "roms" / "program.rom"
        self.emulator_source = self.toy_compiler_path / "pc_emulator_main.cpp"
        self.emulator_exe = self.toy_compiler_path / "build" / "pc_emulator.exe"
        self.emulator_output_dir = self.toy_compiler_path / "build" / "pc_emulator"

    def _needs_emulator_rebuild(self):
        if not self.emulator_exe.exists():
            return True

        source_files = [
            self.emulator_source,
            self.toy_compiler_path / "emu.cpp",
            self.toy_compiler_path / "emu.h",
            self.toy_compiler_path / "definitions.h",
        ]
        exe_mtime = self.emulator_exe.stat().st_mtime
        return any(path.exists() and path.stat().st_mtime > exe_mtime for path in source_files)

    def _build_emulator(self):
        if not self.emulator_source.exists():
            print(f"❌ Emulator source not found: {self.emulator_source}")
            return False

        if not self._needs_emulator_rebuild():
            return True

        self.emulator_exe.parent.mkdir(parents=True, exist_ok=True)

        compiler_candidates = []
        if shutil.which("g++"):
            compiler_candidates.append([
                "g++",
                "-std=c++17",
                "-O2",
                str(self.emulator_source),
                str(self.toy_compiler_path / "emu.cpp"),
                "-o",
                str(self.emulator_exe),
            ])
        if shutil.which("clang++"):
            compiler_candidates.append([
                "clang++",
                "-std=c++17",
                "-O2",
                str(self.emulator_source),
                str(self.toy_compiler_path / "emu.cpp"),
                "-o",
                str(self.emulator_exe),
            ])
        if shutil.which("cl"):
            compiler_candidates.append([
                "cl",
                "/std:c++17",
                "/O2",
                "/EHsc",
                str(self.emulator_source),
                str(self.toy_compiler_path / "emu.cpp"),
                f"/Fe:{self.emulator_exe}",
            ])

        if not compiler_candidates:
            print("❌ No C++ compiler found. Install g++, clang++, or use a Visual Studio developer prompt.")
            return False

        last_error = None
        for command in compiler_candidates:
            result = subprocess.run(
                command,
                cwd=str(self.toy_compiler_path),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0 and self.emulator_exe.exists():
                return True

            last_error = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")

        print("❌ Failed to build PC emulator")
        if last_error:
            print(last_error.strip())
        return False
        
    def compile(self, source_file):
        """Compile source file to ROM.
        
        Args:
            source_file: Path to .txt source file (relative to compiler root)
            
        Returns:
            Path to generated ROM file, or None if compilation failed
        """
        source_path = self.toy_compiler_path / source_file
        if not source_path.exists():
            print(f"❌ Source file not found: {source_path}")
            return None
        
        try:
            result = subprocess.run(
                [sys.executable, str(self.main_script), str(source_path)],
                cwd=str(self.toy_compiler_path),
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                print(f"❌ Compilation failed!")
                print(f"Stderr: {result.stderr}")
                return None
            
            if not self.rom_path.exists():
                print(f"❌ ROM file not generated")
                return None
            
            rom_size = self.rom_path.stat().st_size
            print(f"✅ Compiled: {source_path.name}")
            print(f"   ROM: {rom_size} bytes")
            return self.rom_path
            
        except subprocess.TimeoutExpired:
            print(f"❌ Compilation timed out")
            return None
        except Exception as e:
            print(f"❌ Compilation error: {e}")
            return None
    
    def run_rom(self, rom_path, expected_return=None):
        """Run ROM in emulator and verify output.
        
        Args:
            rom_path: Path to ROM file
            expected_return: Expected return value (int), or None to skip verification
            
        Returns:
            (success: bool, return_value: int or None)
        """
        if not rom_path.exists():
            print(f"❌ ROM not found: {rom_path}")
            return False, None

        if not self._build_emulator():
            return False, None

        self.emulator_output_dir.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [
                str(self.emulator_exe),
                "--rom",
                str(rom_path),
                "--output-dir",
                str(self.emulator_output_dir),
                "--frames",
                "1",
            ],
            cwd=str(self.toy_compiler_path),
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            print("❌ Emulator execution failed")
            if result.stdout:
                print(result.stdout)
            if result.stderr:
                print(result.stderr)
            return False, None

        if result.stdout:
            print(result.stdout.strip())

        match = re.search(r"return=(-?\d+)", result.stdout)
        if not match:
            print("❌ Emulator output did not include a return value")
            return False, None

        return_value = int(match.group(1))
        ppm_path = self.emulator_output_dir / "frame.ppm"
        if ppm_path.exists():
            print(f"   Framebuffer image: {ppm_path}")

        return True, return_value
    
    def test_case(self, name, source_file, expected_return=None, expected_output=None):
        """Run a complete test case.
        
        Args:
            name: Test name for reporting
            source_file: Path to source file
            expected_return: Expected main() return value
            expected_output: Expected stdout (if applicable)
            
        Returns:
            (passed: bool, rom_path: Path)
        """
        print(f"\n{'='*60}")
        print(f"TEST: {name}")
        print(f"{'='*60}")
        
        rom_path = self.compile(source_file)
        if rom_path is None:
            print(f"❌ {name}: FAILED (compilation)")
            return False, None
        
        # Try to run in emulator
        success, return_val = self.run_rom(rom_path, expected_return)
        
        if success is None:
            # Emulator not available, but compilation succeeded
            print(f"⚠️  {name}: COMPILED (emulator not available)")
            return None, rom_path
        
        if success:
            if expected_return is not None:
                if return_val == expected_return:
                    print(f"✅ {name}: PASSED")
                    return True, rom_path
                else:
                    print(f"❌ {name}: FAILED (got {return_val}, expected {expected_return})")
                    return False, rom_path
            else:
                print(f"✅ {name}: PASSED")
                return True, rom_path
        else:
            print(f"❌ {name}: FAILED (execution)")
            return False, rom_path

def main():
    # Get the toy-compiler directory (parent of test_runner.py's parent)
    script_dir = Path(__file__).parent
    if script_dir.name == "toy-compiler":
        compiler_path = script_dir
    else:
        compiler_path = script_dir / "toy-compiler"
    
    runner = EmulatorTestRunner(compiler_path)
    
    # Test suite
    tests = [
        ("1-arg function", "examples/test_1arg.txt", 50),
        ("3-arg function", "examples/test_3arg.txt", 60),
        ("4-arg function", "examples/test_4arg.txt", 100),
        ("5-arg function", "examples/test_5arg.txt", 15),
        ("Local variables", "examples/test_globals.txt", 30),
        ("Nested calls", "examples/test_nested.txt", 17),
        ("With ops function", "examples/test_with_ops.txt", 60),
    ]
    
    results = {}
    for name, source, expected in tests:
        passed, rom_path = runner.test_case(name, source, expected)
        results[name] = (passed, rom_path)
    
    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    
    passed_count = sum(1 for p, _ in results.values() if p is True)
    failed_count = sum(1 for p, _ in results.values() if p is False)
    untested_count = sum(1 for p, _ in results.values() if p is None)
    
    for name, (passed, rom_path) in results.items():
        if passed is True:
            status = "✅ PASSED"
        elif passed is False:
            status = "❌ FAILED"
        else:
            status = "⚠️  COMPILED"
        print(f"{status:20} {name}")
    
    print(f"\nTotal: {passed_count} passed, {failed_count} failed, {untested_count} compiled")

if __name__ == "__main__":
    main()
