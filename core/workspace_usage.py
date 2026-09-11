"""Rate-limit snapshots from existing session owners; never start a provider."""
import asyncio
import time
import math
from datetime import datetime


def normalize_limits(provider, result, now):
    windows = {}
    if provider == 'claude':
        if result.get('rate_limits_available') is not True:
            return None
        for name in ('five_hour', 'seven_day'):
            value = (result.get('rate_limits') or {}).get(name)
            if not isinstance(value, dict) or value.get('utilization') is None:
                continue
            reset = value.get('resets_at')
            windows[name] = {'used_percentage': value['utilization'],
                'resets_at': datetime.fromisoformat(reset.replace('Z', '+00:00')).timestamp() if reset else None,
                'observed_at': now}
    else:
        bucket = next((item for item in result.get('limits', []) if item.get('id') == 'codex'), None)
        if bucket is None:
            return None
        for slot, fallback in (('primary', 'five_hour'), ('secondary', 'seven_day')):
            value = bucket.get(slot)
            if not isinstance(value, dict) or value.get('usedPercent') is None:
                continue
            minutes = value.get('windowDurationMins')
            name = ('seven_day' if minutes > 1440 else 'five_hour') if minutes else fallback
            windows[name] = {'used_percentage': value['usedPercent'],
                'resets_at': value.get('resetsAt'), 'observed_at': now}
    windows = {name: value for name, value in windows.items()
        if type(value['used_percentage']) in (int, float)
        and math.isfinite(value['used_percentage']) and value['used_percentage'] >= 0}
    if not windows:
        return None
    return {'available': True, 'source': provider+'-workspace', 'updated_at': now, **windows}


class WorkspaceUsage:
    def __init__(self):
        self.data = {}
        self.next_refresh = 0
        self.task = None

    def snapshot(self, sessions):
        if time.monotonic() >= self.next_refresh and (self.task is None or self.task.done()):
            self.next_refresh = time.monotonic() + 30
            self.task = asyncio.create_task(self.refresh(list(sessions.values())))
        return dict(self.data)

    async def refresh(self, sessions):
        seen = set()
        for owner, provider in sessions:
            if provider in seen or provider not in {'claude', 'codex'}:
                continue
            rpc = getattr(owner, 'rpc', None) or getattr(getattr(getattr(owner, 'client', None), 'transport', None), 'rpc', None)
            if owner.state not in {'ready', 'running'} or getattr(rpc, 'suspended', False):
                continue
            method = getattr(owner, 'account_rate_limits', None)
            if not callable(method):
                continue
            seen.add(provider)
            try:
                result = await asyncio.wait_for(method(), timeout=8)
                normalized = normalize_limits(provider, result, time.time())
                if normalized:
                    self.data[provider] = normalized
            except Exception:
                # Keep the prior observation's timestamp; failure is not freshness.
                pass
