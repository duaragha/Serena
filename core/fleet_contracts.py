"""Backward-compatibility shim for fleet.contracts."""
import sys
import fleet.contracts as _mod
from fleet.contracts import *

# Ensure module attributes and sys.modules compatibility
sys.modules[__name__] = _mod
