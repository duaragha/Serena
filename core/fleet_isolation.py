"""Backward-compatibility shim for fleet.isolation."""
import sys
import fleet.isolation as _mod
from fleet.isolation import *

# Ensure module attributes and sys.modules compatibility
sys.modules[__name__] = _mod
