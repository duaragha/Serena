"""Backward-compatibility shim for fleet.store."""
import sys
import fleet.store as _mod
from fleet.store import *

# Ensure module attributes and sys.modules compatibility
sys.modules[__name__] = _mod
