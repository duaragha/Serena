"""Backward-compatibility shim for fleet.dag."""
import sys
import fleet.dag as _mod
from fleet.dag import *

# Ensure module attributes and sys.modules compatibility
sys.modules[__name__] = _mod
