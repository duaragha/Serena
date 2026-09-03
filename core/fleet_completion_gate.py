"""Backward-compatibility shim for fleet.completion_gate."""
import sys
import fleet.completion_gate as _mod
from fleet.completion_gate import *

# Ensure module attributes and sys.modules compatibility
sys.modules[__name__] = _mod
