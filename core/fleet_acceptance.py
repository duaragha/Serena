"""Backward-compatibility shim for fleet.acceptance."""
import sys
import fleet.acceptance as _mod
from fleet.acceptance import *

# Ensure module attributes and sys.modules compatibility
sys.modules[__name__] = _mod
