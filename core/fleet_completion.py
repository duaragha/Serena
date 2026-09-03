"""Backward-compatibility shim for fleet.completion."""
import sys
import fleet.completion as _mod
from fleet.completion import *

# Ensure module attributes and sys.modules compatibility
sys.modules[__name__] = _mod
