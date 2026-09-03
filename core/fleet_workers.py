"""Backward-compatibility shim for fleet.workers."""
import sys
import fleet.workers as _mod
from fleet.workers import *

# Ensure module attributes and sys.modules compatibility
sys.modules[__name__] = _mod
