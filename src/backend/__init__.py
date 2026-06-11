"""
Backend Module

This module contains target-specific code generation:
- EmuBackend: 16-bit emulator ROM backend
- core: backend-agnostic analysis and allocation helpers
"""

from .EmuBackend import EmuBackend
from .core import (
	analyze_liveness,
	allocate_function,
	FunctionFrame,
)

__all__ = [
	'EmuBackend',
	'analyze_liveness',
	'allocate_function',
	'FunctionFrame',
]