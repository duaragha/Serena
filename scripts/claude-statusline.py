#!/usr/bin/env python3
"""Claude Code custom statusline (Windows / cross-platform)"""
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

# --- Time format helper (Windows uses %#I, Unix uses %-I) ---
HOUR_FMT = '%#I' if os.name == 'nt' else '%-I'

def fmt_time(ts, with_day=False):
    if not ts:
        return '?'
    try:
        d = datetime.fromtimestamp(ts)
        fmt = f'%a {HOUR_FMT}:%M %p' if with_day else f'{HOUR_FMT}:%M %p'
        return d.strftime(fmt)
    except Exception:
        return '?'

# --- Claude data ---
cwd = data.get('cwd', '')
parts = cwd.replace('\\', '/').rstrip('/').split('/')
dir_short = '/'.join(parts[-2:]) if len(parts) >= 2 else cwd

model = (data.get('model') or {}).get('display_name') or (data.get('model') or {}).get('id') or '?'
model = re.sub(r' \(.*\)', '', model)
version = data.get('version', '?')
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

rl = data.get('rate_limits') or {}
five = rl.get('five_hour') or {}
seven = rl.get('seven_day') or {}
rate_5h = round(five.get('used_percentage', 0))
rate_7d = round(seven.get('used_percentage', 0))
reset_5h = five.get('resets_at', 0)
reset_7d = seven.get('resets_at', 0)
reset_5h_time = fmt_time(reset_5h)
reset_7d_time = fmt_time(reset_7d, with_day=True)

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

# --- Codex model from config.toml ---
codex_dir = Path.home() / '.codex'
cx_model = 'codex'
try:
    cfg = (codex_dir / 'config.toml').read_text(encoding='utf-8', errors='ignore')
    m = re.search(r'^\s*model\s*=\s*"?([^"#\n]+)"?', cfg, re.M)
    if m:
        cx_model = m.group(1).strip()
except Exception:
    pass

# --- Codex CLI version + usage from session files ---
cx_version = '?'
cx_5h = cx_7d = 0
cx_5h_reset = cx_7d_reset = 0
cx_5h_time = cx_7d_time = '?'
cx_have = False

sessions_dir = codex_dir / 'sessions'
if sessions_dir.exists():
    jsonl_files = []
    try:
        for p in sessions_dir.rglob('*.jsonl'):
            try:
                jsonl_files.append((p.stat().st_mtime, p))
            except Exception:
                pass
    except Exception:
        pass
    jsonl_files.sort(reverse=True)

    if jsonl_files:
        try:
            with open(jsonl_files[0][1], 'r', encoding='utf-8', errors='ignore') as f:
                first = f.readline()
                if first:
                    j = json.loads(first)
                    cx_version = (j.get('payload') or {}).get('cli_version') or '?'
        except Exception:
            pass

    for _, fpath in jsonl_files[:100]:
        try:
            populated = []
            with open(fpath, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if 'token_count' not in line:
                        continue
                    try:
                        j = json.loads(line)
                    except Exception:
                        continue
                    rlc = ((j.get('payload') or {}).get('rate_limits') or {})
                    if rlc.get('primary'):
                        populated.append(j)
            if populated:
                last = populated[-1]
                rlc = last['payload']['rate_limits']
                prim = rlc.get('primary') or {}
                sec = rlc.get('secondary') or {}
                cx_5h = round(prim.get('used_percent', 0))
                cx_7d = round(sec.get('used_percent', 0))
                cx_5h_reset = prim.get('resets_at', 0)
                cx_7d_reset = sec.get('resets_at', 0)
                cx_5h_time = fmt_time(cx_5h_reset)
                cx_7d_time = fmt_time(cx_7d_reset, with_day=True)
                cx_have = True
                break
        except Exception:
            continue

# --- Countdown ---
def countdown(target, show_days=False):
    diff = target - time.time()
    if diff <= 0:
        return '0H:0M'
    if show_days:
        d = int(diff // 86400)
        h = int((diff % 86400) // 3600)
        m = int((diff % 3600) // 60)
        return f"{d}D:{h}H:{m}M"
    h = int(diff // 3600)
    m = int((diff % 3600) // 60)
    return f"{h}H:{m}M"

cd_5h = countdown(reset_5h)
cd_7d = countdown(reset_7d, show_days=True)
cx_cd_5h = countdown(cx_5h_reset)
cx_cd_7d = countdown(cx_7d_reset, show_days=True)

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
h5_bar = render_bar(rate_5h)
d7_bar = render_bar(rate_7d)
cx_5h_bar = render_bar(cx_5h)
cx_7d_bar = render_bar(cx_7d)

# --- Colors ---
O = '\033[38;2;255;175;80m'
B = '\033[38;2;120;180;255m'
W = '\033[38;2;220;220;220m'
D = '\033[38;2;220;220;220m'
G = '\033[38;2;120;220;140m'
L = '\033[38;2;160;160;160m'
S = '\033[38;2;255;130;200m'
C = '\033[38;2;130;220;230m'
T = '\033[38;2;255;200;120m'
R = '\033[0m'

# --- Output ---
sys.stdout.write(
    f"{S}Serena{R} {D}│{R} {C}{dir_short}{R} {D}│{R} {O}{model}{R} {D}│{R} {B}{cx_model}{R} {D}│{R} {O}v{version}{R} {D}│{R} {B}v{cx_version}{R}\n"
    f"{G}{cost_fmt}{R} {D}│{R} {L}CW {cw_bar} {W}{cw_pct}%{R} {D}│{R} {W}{used_k}k/{total_k}k{R} {D}│{R} {L}SD {W}{dur_fmt}{R}\n"
    f"{O}Claude{R} {L}5H{R} {h5_bar} {W}{rate_5h}%{R} {D}│{R} {T}{cd_5h} │ {reset_5h_time}{R}  {D}║{R}  {L}7D{R} {d7_bar} {W}{rate_7d}%{R} {D}│{R} {T}{cd_7d} │ {reset_7d_time}{R}\n"
)
if cx_have:
    sys.stdout.write(
        f"{B}Codex{R}  {L}5H{R} {cx_5h_bar} {W}{cx_5h}%{R} {D}│{R} {T}{cx_cd_5h} │ {cx_5h_time}{R}  {D}║{R}  {L}7D{R} {cx_7d_bar} {W}{cx_7d}%{R} {D}│{R} {T}{cx_cd_7d} │ {cx_7d_time}{R}\n"
    )
sys.exit(0)
