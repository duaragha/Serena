"""Backward-compatibility shim for fleet.mcp."""
import sys
import fleet.mcp as _mod
from fleet.mcp import *

# Ensure module attributes and sys.modules compatibility
sys.modules[__name__] = _mod
