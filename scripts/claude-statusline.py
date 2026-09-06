#!/usr/bin/env python3
"""Claude Code custom statusline (Windows / cross-platform).

One line, matching the bash statusline on the laptop. Three agents share this
screen and whoever prints most wins least: Claude's four lines pushed the Codex
and Gemini panes down far enough that the pane got picked least often.

Everything cut from here is in the app's usage section, which is where it is
actually read -- rate-limit bars and countdowns for both agents, the model
names, and the CLI versions. What stays is what is true only of THIS session
and appears nowhere else: where it is running, what it has cost, how full the
context is, and how long it has been going.

The Serena app tap below is untouched. Claude's rate limits exist nowhere but
that stdin payload, so it still forwards them along with the model and version
the usage section names. It stopped printing them; it did not stop knowing
them.
"""
import json
import sys
import os
import re
import time
from datetime import datetime
from pathlib import Path

# Force UTF-8 stdout (Windows defaults to cp1252)
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

# --- Read stdin ---
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}

# --- Claude data ---
cwd = data.get('cwd', '')
parts = cwd.replace('\\', '/').rstrip('/').split('/')
dir_short = '/'.join(parts[-2:]) if len(parts) >= 2 else cwd

cost = (data.get('cost') or {}).get('total_cost_usd', 0)
cost_fmt = f"${cost:.2f}"
dur_ms = (data.get('cost') or {}).get('total_duration_ms', 0)
dur_s = int(dur_ms / 1000)
dur_fmt = f"{dur_s // 3600}H:{(dur_s % 3600) // 60}M"

cw = data.get('context_window') or {}
cw_pct = round(cw.get('used_percentage', 0))
ctx_size = cw.get('context_window_size', 1000000)
used_k = cw_pct * ctx_size // 100 // 1000
total_k = ctx_size // 1000

# --- Serena app tap ---
# Claude's rate limits exist nowhere but this payload. Codex writes its usage
# into session files the app can read whenever it likes; Claude's arrive here
# and are gone when this process exits. Without this the app shows Claude as
# "waiting" forever, which is exactly what Windows was doing while the Linux
# statusline (a bash script) had been taping the same numbers all along.
try:
    for _candidate in (
        Path.home() / 'Projects' / 'serena',                  # Windows PC
        Path.home() / 'Documents' / 'Projects' / 'serena',    # Linux laptop
    ):
        if (_candidate / 'core' / 'usage_aggregator.py').is_file():
            sys.path.insert(0, str(_candidate))
            break
    from core.usage_aggregator import record_statusline

    record_statusline(data)
except Exception:
    pass  # A status line that cannot draw is worse than one without the tap.

# --- Gradient bar ---
def render_bar(pct, width=10):
    filled = min(pct * width // 100, width)
    out = ''
    for i in range(width):
        if i < filled:
            pos = (i + 1) * 100 // width
            if pos <= 50:
                r = 255 * pos // 50
                g = 255
            else:
                r = 255
                g = 255 * (100 - pos) // 50
            out += f'\033[38;2;{r};{g};0m━'
        else:
            out += '\033[38;2;50;50;50m─'
    out += '\033[0m'
    return out

cw_bar = render_bar(cw_pct)

# --- Colors ---
W = '\033[38;2;220;220;220m'   # soft white
D = '\033[38;2;220;220;220m'   # separators
G = '\033[38;2;120;220;140m'   # soft green
L = '\033[38;2;160;160;160m'   # label gray
S = '\033[38;2;255;130;200m'   # hot pink for serena
C = '\033[38;2;130;220;230m'   # cyan for the directory
R = '\033[0m'

# --- Output ---
sys.stdout.write(
    f"{S}Serena{R} {D}\u2502{R} {C}{dir_short}{R} {D}\u2502{R} {G}{cost_fmt}{R} {D}\u2502{R} "
    f"{L}CW{R} {cw_bar} {W}{cw_pct}%{R} {D}\u2502{R} {W}{used_k}k/{total_k}k{R} "
    f"{D}\u2502{R} {L}SD{R} {W}{dur_fmt}{R}\n"
)
sys.exit(0)
