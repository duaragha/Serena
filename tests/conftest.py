import sys
from pathlib import Path

_LEGACY_DESKTOP = Path(__file__).resolve().parent.parent / "archive" / "desktop-gtk-legacy"
if _LEGACY_DESKTOP.is_dir() and str(_LEGACY_DESKTOP) not in sys.path:
    sys.path.insert(0, str(_LEGACY_DESKTOP))
