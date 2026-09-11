"""Compatibility import for Fleet's shared portable process lock."""

from core.file_lock import exclusive_lock

__all__ = ["exclusive_lock"]
