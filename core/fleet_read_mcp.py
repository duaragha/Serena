"""Backward-compatibility shim for fleet.read_mcp."""
import sys
import fleet.read_mcp as _mod
from fleet.read_mcp import *

# Ensure module attributes and sys.modules compatibility
sys.modules[__name__] = _mod
