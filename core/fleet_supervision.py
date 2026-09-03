"""Backward-compatibility shim for fleet.supervision."""
import sys
import fleet.supervision as _mod
from fleet.supervision import *

# Ensure module attributes and sys.modules compatibility
sys.modules[__name__] = _mod
