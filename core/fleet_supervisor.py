"""Backward-compatibility shim for fleet.supervisor."""
import sys
import fleet.supervisor as _mod
from fleet.supervisor import *

# Ensure module attributes and sys.modules compatibility
sys.modules[__name__] = _mod
