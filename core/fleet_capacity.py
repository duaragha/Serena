"""Backward-compatibility shim for fleet.capacity."""
import sys
import fleet.capacity as _mod
from fleet.capacity import *

# Ensure module attributes and sys.modules compatibility
sys.modules[__name__] = _mod
