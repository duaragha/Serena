"""Backward-compatibility shim for fleet.policy."""
import sys
import fleet.policy as _mod
from fleet.policy import *

# Ensure module attributes and sys.modules compatibility
sys.modules[__name__] = _mod
