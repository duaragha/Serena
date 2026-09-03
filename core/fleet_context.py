"""Backward-compatibility shim for fleet.context."""
import sys
import fleet.context as _mod
from fleet.context import *

# Ensure module attributes and sys.modules compatibility
sys.modules[__name__] = _mod
