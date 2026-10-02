"""Standalone Windows game watcher. Uses only the Python standard library.

CPU changes are journaled before application and restored after the last game
exits. Memory changes are a separate, offline maintenance operation: the watcher
never stops guests, changes RAM, kills containers, or changes the PC power plan.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import ntpath
import os
import re
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

BELOW_NORMAL = 0x4000
NORMAL_PRIORITY = 0x20
# The Store API calls some software (including Wallpaper Engine) "game".
# Its software genres take precedence over that broad product type.
STEAM_SOFTWARE_GENRES = {str(value) for value in range(50, 61)}
STEAM_NON_GAMES = {'431960', '228980'}
STEAM_TYPES_CACHE = 'steam-types-v2.json'
STEAM_GAMES_CACHE = 'steam-games-v2.json'
HELPERS = re.compile(
    r"^(steam.*|epicwebhelper|crashreport.*|.*crashhandler.*|ueprereq.*|"
    r"setup.*|unins.*|installer.*|.*anticheat.*|.*_loader|launcher|"
    r"configuration|configtool|dxsetup|vcredist.*|vc_redist.*)\.exe$", re.I)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8-sig'))


def path_key(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(os.path.expandvars(value)))


def affinity_mask(system_mask: int, cpus: int) -> int:
    """The highest `cpus` logical processors the system actually offers.

    This applies only where Windows permits affinity changes. VirtualBox's
    protected worker processes may reject it; never infer exclusive game cores.
    """
    available = [bit for bit in range(system_mask.bit_length()) if system_mask >> bit & 1]
    if cpus >= len(available):
        return system_mask
    return sum(1 << bit for bit in available[-cpus:])


@dataclass(frozen=True)
class Process:
    pid: int
    path: str
    created: int
    cpu_ticks: int = 0


def game_names(processes: list[Process], rules: list[dict]) -> list[str]:
    names = set()
    for process in processes:
        actual = path_key(process.path)
        exe = ntpath.basename(actual)
        for rule in rules:
            root = path_key(rule['root'])
            try:
                inside = ntpath.commonpath([actual, root]) == root
            except ValueError:
                inside = False
            if not inside:
                continue
            allowed = rule.get('executables', [])
            if allowed:
                matched = exe in {x.lower() for x in allowed}
            else:
                matched = exe.endswith('.exe') and not HELPERS.match(exe)
            if matched:
                names.add(rule['name'])
    return sorted(names)


class Windows:
    def __init__(self):
        if os.name != 'nt':
            raise RuntimeError('The watcher runs on Windows; its policy tests run on any OS.')
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.psapi = ctypes.WinDLL('psapi', use_last_error=True)
        k = self.kernel
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                               wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k.GetPriorityClass.argtypes = [wintypes.HANDLE]
        k.GetPriorityClass.restype = wintypes.DWORD
        k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t),
                                             ctypes.POINTER(ctypes.c_size_t)]
        k.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
        k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        k.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.psapi.EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD)]
        self.previous_times = None
        self.previous_processes = {}
        self.previous_clock = None

    @staticmethod
    def ticks(value):
        return (value.dwHighDateTime << 32) | value.dwLowDateTime

    def process(self, pid: int) -> Process | None:
        handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            path = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(path))
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                return None
            created, exited, kernel, user = [wintypes.FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                                ctypes.byref(kernel), ctypes.byref(user)):
                return None
            return Process(pid, path.value, self.ticks(created), self.ticks(kernel) + self.ticks(user))
        finally:
            self.kernel.CloseHandle(handle)

    def processes(self) -> list[Process]:
        pids = (wintypes.DWORD * 65536)()
        size = wintypes.DWORD()
        if not self.psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        if size.value >= ctypes.sizeof(pids):
            raise RuntimeError('Process inventory exceeded the buffer; keeping existing limits.')
        return [p for pid in pids[:size.value // ctypes.sizeof(wintypes.DWORD)]
                if (p := self.process(pid)) is not None]

    def priority(self, process: Process, value: int | None = None) -> int | None:
        current = self.process(process.pid)
        if not current or current.created != process.created:
            return None
        handle = self.kernel.OpenProcess(0x1000 | (0x0200 if value is not None else 0), False, process.pid)
        if not handle:
            if ctypes.get_last_error() == 5:
                return None  # VirtualBox hardening protects some helper processes.
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if value is not None and not self.kernel.SetPriorityClass(handle, value):
                if ctypes.get_last_error() == 5:
                    return None  # No priority mutation occurred.
                raise ctypes.WinError(ctypes.get_last_error())
            result = self.kernel.GetPriorityClass(handle)
            if not result:
                raise ctypes.WinError(ctypes.get_last_error())
            return result
        finally:
            self.kernel.CloseHandle(handle)

    def system_affinity(self) -> int:
        process_mask, system_mask = ctypes.c_size_t(), ctypes.c_size_t()
        if not self.kernel.GetProcessAffinityMask(self.kernel.GetCurrentProcess(),
                                                  ctypes.byref(process_mask), ctypes.byref(system_mask)):
            raise ctypes.WinError(ctypes.get_last_error())
        return system_mask.value

    def affinity(self, process: Process, mask: int | None = None) -> int | None:
        current = self.process(process.pid)
        if not current or current.created != process.created:
            return None
        handle = self.kernel.OpenProcess(0x1000 | (0x0200 if mask is not None else 0), False, process.pid)
        if not handle:
            if ctypes.get_last_error() == 5:
                return None  # VirtualBox hardening protects some helper processes.
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if mask is not None and not self.kernel.SetProcessAffinityMask(handle, mask):
                if ctypes.get_last_error() == 5:
                    return None  # No affinity mutation occurred.
                raise ctypes.WinError(ctypes.get_last_error())
            process_mask, system_mask = ctypes.c_size_t(), ctypes.c_size_t()
            if not self.kernel.GetProcessAffinityMask(handle, ctypes.byref(process_mask),
                                                      ctypes.byref(system_mask)):
                raise ctypes.WinError(ctypes.get_last_error())
            return process_mask.value
        finally:
            self.kernel.CloseHandle(handle)

    def sample(self, processes: list[Process], rules: list[dict]) -> dict:
        class MemoryStatus(ctypes.Structure):
            _fields_ = [('length', wintypes.DWORD), ('load', wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong) for name in (
                    'total', 'available', 'page_total', 'page_available',
                    'virtual_total', 'virtual_available', 'extended')]
        memory = MemoryStatus()
        memory.length = ctypes.sizeof(memory)
        if not self.kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
            raise ctypes.WinError(ctypes.get_last_error())
        idle, kernel, user = [wintypes.FILETIME() for _ in range(3)]
        if not self.kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        values = (self.ticks(idle), self.ticks(kernel) + self.ticks(user))
        clock = time.monotonic()
        cpu = background = None
        if self.previous_times:
            idle_delta = values[0] - self.previous_times[0]
            total_delta = values[1] - self.previous_times[1]
            cpu = max(0, min(100, 100 * (1 - idle_delta / max(1, total_delta))))
            game_ticks = 0
            for p in processes:
                before = self.previous_processes.get((p.pid, p.created))
                if before is not None and game_names([p], rules):
                    game_ticks += max(0, p.cpu_ticks - before)
            game_cpu = 100 * game_ticks / max(1, (clock - self.previous_clock) * 10_000_000 * (os.cpu_count() or 1))
            background = max(0, cpu - game_cpu)
        self.previous_times, self.previous_clock = values, clock
        self.previous_processes = {(p.pid, p.created): p.cpu_ticks for p in processes}
        return {'total_ram_gib': round(memory.total / 2**30, 2),
                'available_ram_gib': round(memory.available / 2**30, 2),
                'total_ram_used_pct': round(100 * (1 - memory.available / memory.total), 1),
                'total_cpu_pct': None if cpu is None else round(cpu, 1),
                'background_cpu_pct': None if background is None else round(background, 1)}

    def lock(self):
        ctypes.set_last_error(0)
        handle = self.kernel.CreateMutexW(None, True, 'Global\\SerenaPcGameMode-' + os.environ.get('USERNAME', 'user'))
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            self.kernel.CloseHandle(handle)
            raise RuntimeError('A game-mode controller is already running.')
        return handle

    def unlock(self, handle):
        self.kernel.ReleaseMutex(handle)
        self.kernel.CloseHandle(handle)


class VBox:
    def __init__(self, executable: str):
        self.executable = os.path.expandvars(executable)

    def call(self, *args):
        result = subprocess.run([self.executable, *map(str, args)], capture_output=True,
                                text=True, timeout=15,
                                creationflags=0x08000000 if os.name == 'nt' else 0)
        if result.returncode:
            raise RuntimeError(f'VBoxManage {args[0]} failed: {result.stderr.strip()[:600]}')
        return result.stdout

    def info(self, name):
        values = {}
        for line in self.call('showvminfo', name, '--machinereadable').splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                values[key] = value.strip('"')
        return values

    def cap(self, name, cap, state):
        if state in ('running', 'paused'):
            self.call('controlvm', name, 'cpuexecutioncap', cap)
        elif state in ('poweroff', 'aborted', 'saved'):
            self.call('modifyvm', name, '--cpuexecutioncap', cap)
        else:
            raise RuntimeError(f'{name} is transitioning ({state}); will retry restoration.')

    def priority(self, name, value, state):
        if state in ('running', 'paused'):
            self.call('controlvm', name, 'vm-process-priority', value)
        elif state in ('poweroff', 'aborted', 'saved'):
            self.call('modifyvm', name, '--vm-process-priority', value)
        else:
            raise RuntimeError(f'{name} is transitioning ({state}); will retry restoration.')
        if self.info(name).get('vmprocpriority') != value:
            raise RuntimeError(f'VM priority verification failed for {name}')


class Controller:
    def __init__(self, config: dict, vbox, windows, state_dir: Path):
        self.config, self.vbox, self.windows = config, vbox, windows
        self.state_dir = state_dir
        self.journal_path = state_dir / 'restore.json'
        self.journal = read_json(self.journal_path, {'vms': {}, 'priorities': {}, 'affinities': {}})
        # A journal written before affinity pinning existed is still valid; anything
        # else is refused rather than overwriting the saved original settings.
        if not isinstance(self.journal, dict) or not (
                {'vms', 'priorities'} <= set(self.journal) <= {'vms', 'priorities', 'affinities'}):
            raise ValueError('Invalid restore journal; refusing to overwrite the original settings.')
        self.journal.setdefault('affinities', {})
        self.last_game_seen = None
        self.unmodifiable_priorities = set()
        self.unmodifiable_affinities = set()

    def save(self):
        write_json(self.journal_path, self.journal)

    @property
    def active(self):
        return bool(self.journal['vms'] or self.journal['priorities'] or self.journal['affinities'])

    def apply(self, processes):
        try:
            for profile in self.config['vms']:
                name = profile['name']
                if name in self.journal['vms']:
                    continue
                info = self.vbox.info(name)
                if info['VMState'] != 'running':
                    continue
                original = int(info['cpuexecutioncap'])
                applied = min(original, int(profile['cpu_cap']))
                self.journal['vms'][name] = {'uuid': info['UUID'], 'original': original, 'applied': applied}
                # Native control changes protected VM worker thread priorities,
                # unlike SetPriorityClass on the accessible launcher alone.
                priority = info.get('vmprocpriority')
                if priority not in {'default', 'flat', 'low', 'normal', 'high'}:
                    raise RuntimeError(f'Cannot read a supported VM priority for {name}')
                self.journal['vms'][name].update(priority_original=priority, priority_applied='low')
                self.save()  # Recovery information must exist before the first mutation.
                self.vbox.cap(info['UUID'], applied, info['VMState'])
                self.vbox.priority(info['UUID'], 'low', info['VMState'])
            headless = path_key(ntpath.join(ntpath.dirname(self.vbox.executable), 'VBoxHeadless.exe'))
            for process in processes:
                if path_key(process.path) != headless:
                    continue
                self.apply_affinity(process)
                self.apply_priority(process)
        except Exception:
            self.restore()
            raise

    def apply_priority(self, process):
        key = str(process.pid)
        if (process.pid, process.created) in self.unmodifiable_priorities:
            return
        entry = self.journal['priorities'].get(key)
        if entry and entry['created'] == process.created:
            return
        original = self.windows.priority(process)
        if original is None:
            return
        # Preserve Idle/BelowNormal if the user already selected either.
        applied = original if original in (0x40, BELOW_NORMAL) else BELOW_NORMAL
        self.journal['priorities'][key] = {'created': process.created, 'path': process.path,
                                           'original': original, 'applied': applied}
        self.save()
        if self.windows.priority(process, applied) is None:
            # Native VM priority and CPU caps still apply. Protection includes
            # the actual CPU-consuming worker, not just helper wrappers.
            self.unmodifiable_priorities.add((process.pid, process.created))
            del self.journal['priorities'][key]
            self.save()
            logging.info('Windows process priority denied for VBox PID %s; native VM priority is used', process.pid)

    def apply_affinity(self, process):
        cpus = int(self.config.get('vm_affinity_cpus', 0))
        if not cpus:
            return
        key = str(process.pid)
        if (process.pid, process.created) in self.unmodifiable_affinities:
            return
        entry = self.journal['affinities'].get(key)
        if entry and entry['created'] == process.created:
            return
        original = self.windows.affinity(process)
        if original is None:
            return
        target = affinity_mask(self.windows.system_affinity(), cpus)
        # Preserve a narrower pin the user already chose, exactly as the CPU caps do.
        applied = original if original & ~target == 0 else target
        self.journal['affinities'][key] = {'created': process.created, 'path': process.path,
                                           'original': original, 'applied': applied}
        self.save()
        if self.windows.affinity(process, applied) is None:
            self.unmodifiable_affinities.add((process.pid, process.created))
            del self.journal['affinities'][key]
            self.save()
            logging.warning('Core affinity denied for VBox PID %s; no exclusive-core guarantee', process.pid)

    def restore(self):
        failures = []
        for name, entry in list(self.journal['vms'].items()):
            try:
                info = self.vbox.info(entry['uuid'])
                if int(info['cpuexecutioncap']) == entry['applied']:
                    self.vbox.cap(entry['uuid'], entry['original'], info['VMState'])
                else:
                    logging.info('Preserving external CPU-cap change for %s', name)
                if 'priority_original' in entry:
                    if info.get('vmprocpriority') == entry['priority_applied']:
                        self.vbox.priority(entry['uuid'], entry['priority_original'], info['VMState'])
                    else:
                        logging.info('Preserving external VM-priority change for %s', name)
                del self.journal['vms'][name]
                self.save()
            except Exception as exc:
                failures.append(str(exc))
        for pid, entry in list(self.journal['priorities'].items()):
            try:
                process = Process(int(pid), entry['path'], entry['created'])
                current = self.windows.priority(process)
                if current == entry['applied']:
                    if self.windows.priority(process, entry['original']) != entry['original']:
                        raise RuntimeError(f'Windows priority restore pending for PID {pid}')
                del self.journal['priorities'][pid]
                self.save()
            except Exception as exc:
                failures.append(str(exc))
        for pid, entry in list(self.journal['affinities'].items()):
            try:
                process = Process(int(pid), entry['path'], entry['created'])
                if self.windows.affinity(process) == entry['applied']:
                    if self.windows.affinity(process, entry['original']) != entry['original']:
                        raise RuntimeError(f'Windows affinity restore pending for PID {pid}')
                del self.journal['affinities'][pid]
                self.save()
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            logging.error('Restoration pending; saved settings retained: %s', '; '.join(failures))
        return failures

    def tick(self, games, processes, now):
        if games and self.config.get('enabled', True):
            self.last_game_seen = now
            self.apply(processes)
        elif self.active and (not self.config.get('enabled', True) or self.last_game_seen is None
                              or now - self.last_game_seen >= self.config['cooldown_seconds']):
            self.restore()
        return self.active


def steam_roots() -> list[str]:
    import winreg
    roots = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
            roots.append(winreg.QueryValueEx(key, 'SteamPath')[0])
    except OSError:
        pass
    for root in list(roots):
        file = Path(root) / 'steamapps' / 'libraryfolders.vdf'
        if file.exists():
            roots.extend(value.replace('\\\\', '\\') for value in
                         re.findall(r'"path"\s+"([^"]+)"', file.read_text(encoding='utf-8')))
    return list({path_key(root): root for root in roots}.values())


def steam_type(app_id):
    request = urllib.request.Request(
        'https://store.steampowered.com/api/appdetails?appids=' + str(int(app_id)),
        headers={'User-Agent': 'SerenaPcGameMode/1.0'})
    with urllib.request.urlopen(request, timeout=3) as response:
        data = json.load(response).get(str(app_id), {})
    if not data.get('success'):
        return None
    product = data.get('data', {})
    if any(str(genre.get('id')) in STEAM_SOFTWARE_GENRES
           for genre in product.get('genres', [])):
        return 'application'
    return product.get('type')


def discover_steam(roots, cache, classifier=steam_type):
    rules = []
    for root in roots:
        for manifest in (Path(root) / 'steamapps').glob('appmanifest_*.acf'):
            text = manifest.read_text(encoding='utf-8')
            values = dict(re.findall(r'"(appid|name|installdir)"\s+"([^"]+)"', text))
            if not all(key in values for key in ('appid', 'name', 'installdir')):
                continue
            app_id = values['appid']
            if not app_id.isdigit() or app_id in STEAM_NON_GAMES:
                continue
            kind = cache.get(app_id)
            if kind is None:
                try:
                    kind = classifier(app_id)
                    if kind is not None:
                        cache[app_id] = kind
                except Exception:
                    logging.warning('Steam classification unavailable for %s; will retry later', app_id)
                    continue
            if kind != 'game':
                continue
            folder = Path(root) / 'steamapps' / 'common' / values['installdir']
            if folder.is_dir():
                rules.append({'name': values['name'], 'root': str(folder), 'steam_app_id': app_id})
    return rules


def load_config(path):
    config = read_json(path)
    if not isinstance(config, dict):
        raise ValueError('A configuration file is required.')
    if not 2 <= config.get('poll_seconds', 5) <= 30:
        raise ValueError('poll_seconds must be between 2 and 30.')
    if not 0 <= config.get('cooldown_seconds', 20) <= 120:
        raise ValueError('cooldown_seconds must be between 0 and 120.')
    cpus = config.get('vm_affinity_cpus')
    if cpus is not None:
        if not isinstance(cpus, int) or isinstance(cpus, bool):
            raise ValueError('vm_affinity_cpus must be an integer count of logical processors.')
        # 0 disables pinning. Two logical processors are always left to the game
        # and Windows, so the guests can never be given the whole machine.
        if cpus and not 1 <= cpus <= max(1, (os.cpu_count() or 2) - 2):
            raise ValueError('vm_affinity_cpus must leave at least two logical processors free.')
    for vm in config['vms']:
        if not 20 <= int(vm['cpu_cap']) <= 100:
            raise ValueError('CPU caps below 20% are deliberately unsupported.')
        if int(vm.get('memory_mib', 4096)) < 4096:
            raise ValueError('Memory profiles below 4 GiB are deliberately unsupported.')
    config.setdefault('poll_seconds', 5)
    config.setdefault('cooldown_seconds', 20)
    return config


def memory_plan(config, vbox, sample):
    changes = []
    reclaimed = 0
    for profile in config['vms']:
        info = vbox.info(profile['name'])
        original = int(info['memory'])
        desired = int(profile['memory_mib'])
        if info['VMState'] == 'running':
            reclaimed += (original - desired) / 1024
        changes.append({'name': profile['name'], 'uuid': info['UUID'], 'state': info['VMState'],
                        'current_memory_mib': original, 'proposed_memory_mib': desired})
    total = sample['total_ram_gib']
    return {'changes': changes, 'requires_offline_guests': True,
            'estimated_ram_freed_gib': round(reclaimed, 2),
            'estimated_background_ram_used_pct': round(sample['total_ram_used_pct'] - 100 * reclaimed / total, 1),
            'estimate_only': True, 'measured': sample}


def apply_memory_offline(config, vbox, state_dir, restore=False):
    backup_path = state_dir / 'memory-original.json'
    backup = read_json(backup_path, {})
    proposals = []
    for profile in config['vms']:
        info = vbox.info(profile['name'])
        if info['VMState'] != 'poweroff':
            raise RuntimeError(f"{profile['name']} must already be gracefully powered off; no VM was stopped or changed.")
        original = int(info['memory'])
        desired = backup.get(info['UUID'], {}).get('memory_mib') if restore else int(profile['memory_mib'])
        if desired is None:
            raise RuntimeError('No saved memory allocation exists for this VM.')
        proposals.append((profile['name'], info['UUID'], original, desired))
    if not restore:
        for name, uuid, original, _ in proposals:
            backup.setdefault(uuid, {'name': name, 'memory_mib': original})
        write_json(backup_path, backup)
    changed = []
    try:
        for name, uuid, original, desired in proposals:
            vbox.call('modifyvm', uuid, '--memory', desired)
            changed.append((uuid, original))
            if int(vbox.info(uuid)['memory']) != desired:
                raise RuntimeError(f'Memory verification failed for {name}')
    except Exception:
        for uuid, original in reversed(changed):
            vbox.call('modifyvm', uuid, '--memory', original)
        raise
    return [{'name': name, 'memory_mib': desired} for name, _, _, desired in proposals]


def watch(config_path, state_dir, windows, vbox, max_seconds=None):
    config = load_config(config_path)
    controller = Controller(config, vbox, windows, state_dir)
    started = time.monotonic()
    last_discovery = float('-inf')
    # Catalogue files are rebuildable. A bad cache must not prevent recovery of
    # saved CPU settings or recognition of explicitly configured games.
    discovered, cached_types = [], {}
    try:
        catalog = read_json(state_dir / STEAM_GAMES_CACHE, {})
        types = read_json(state_dir / STEAM_TYPES_CACHE, {})
        if isinstance(catalog, dict) and isinstance(catalog.get('games', []), list):
            discovered = catalog.get('games', [])
        if isinstance(types, dict):
            cached_types = types
    except (OSError, ValueError):
        logging.exception('Ignoring invalid rebuildable Steam cache')
    previous_games = None
    try:
        while max_seconds is None or time.monotonic() - started < max_seconds:
            config = load_config(config_path)
            controller.config = config
            now = time.monotonic()
            processes = windows.processes()
            explicit = config.get('games', [])
            # Explicit executable rules override broader library-folder discovery.
            roots = {path_key(rule['root']) for rule in explicit}
            rules = explicit + [rule for rule in discovered if path_key(rule['root']) not in roots]
            games = game_names(processes, rules)
            # Recovery takes place before discovery/network calls on a fresh launch.
            active = controller.tick(games, processes, now)
            sample = windows.sample(processes, rules)
            status = {'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'pid': os.getpid(),
                      'enabled': config.get('enabled', True), 'active': active, 'games': games,
                      'targets': config['targets'], 'memory_changes_automatic': False,
                      'sample': sample, 'game_rules': rules,
                      'pending_restoration': controller.journal,
                      'affinity_denied_pids': sorted(pid for pid, _ in controller.unmodifiable_affinities),
                      'ram_target_met_without_game': not games and sample['total_ram_used_pct'] <= config['targets']['background_ram_pct'],
                      'cpu_target_met': sample['background_cpu_pct'] is not None and sample['background_cpu_pct'] <= config['targets']['background_cpu_pct']}
            write_json(state_dir / 'status.json', status)
            if not config.get('enabled', True) and not active:
                break
            if games != previous_games:
                logging.info('Games=%s; CPU mode=%s', games, active)
                previous_games = games
            if config.get('discover_steam', True) and not games and now - last_discovery >= 300:
                try:
                    discovered = discover_steam(steam_roots(), cached_types)
                    write_json(state_dir / STEAM_GAMES_CACHE, {'games': discovered})
                    write_json(state_dir / STEAM_TYPES_CACHE, cached_types)
                except Exception:
                    logging.exception('Keeping previous game catalogue after discovery failure')
                last_discovery = now
            time.sleep(config['poll_seconds'])
    finally:
        failures = controller.restore()
        if failures:
            raise RuntimeError('CPU restoration will be retried by the watchdog: ' + '; '.join(failures))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('command', choices=['watch', 'status', 'discover', 'exercise', 'restore',
                                           'memory-plan', 'memory-apply-offline', 'memory-restore-offline'])
    parser.add_argument('--seconds', type=int, default=30)
    parser.add_argument('--max-seconds', type=int)
    args = parser.parse_args(argv)
    state_dir = args.state_dir or Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'SerenaGameMode'
    config_path = args.config or state_dir / 'config.json'
    state_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(
        state_dir / 'watcher.log', maxBytes=1_000_000, backupCount=2, encoding='utf-8')])
    if args.command == 'status':
        print(json.dumps(read_json(state_dir / 'status.json', {'running': False}), indent=2))
        return 0
    config = load_config(config_path)
    windows = Windows()
    vbox = VBox(config['vboxmanage'])
    if args.command == 'memory-plan':
        print(json.dumps(memory_plan(config, vbox, windows.sample(windows.processes(), [])), indent=2))
        return 0
    if args.command == 'discover':
        cache = read_json(state_dir / STEAM_TYPES_CACHE, {})
        games = discover_steam(steam_roots(), cache)
        write_json(state_dir / STEAM_TYPES_CACHE, cache)
        write_json(state_dir / STEAM_GAMES_CACHE, {'games': games})
        print(json.dumps(games, indent=2))
        return 0
    handle = windows.lock()
    try:
        if args.command == 'watch':
            watch(config_path, state_dir, windows, vbox, args.max_seconds)
        elif args.command in ('memory-apply-offline', 'memory-restore-offline'):
            print(json.dumps(apply_memory_offline(config, vbox, state_dir,
                                                  args.command == 'memory-restore-offline'), indent=2))
        else:
            controller = Controller(config, vbox, windows, state_dir)
            if args.command == 'restore':
                failures = controller.restore()
                if failures:
                    raise RuntimeError('; '.join(failures))
                print(json.dumps({'restored': True}))
            else:
                if not 5 <= args.seconds <= 120:
                    raise ValueError('Exercise duration must be between 5 and 120 seconds.')
                if controller.active:
                    raise RuntimeError('Recover the existing journal before exercising.')
                processes = windows.processes()
                if game_names(processes, config.get('games', [])):
                    raise RuntimeError('A configured game is already running; skipping maintenance exercise.')
                samples = []
                try:
                    controller.apply(processes)
                    for _ in range(args.seconds // 5):
                        time.sleep(5)
                        samples.append(windows.sample(windows.processes(), []))
                finally:
                    failures = controller.restore()
                    if failures:
                        raise RuntimeError('; '.join(failures))
                print(json.dumps({'restored': not controller.active, 'samples': samples}, indent=2))
    finally:
        windows.unlock(handle)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        logging.exception('Game mode stopped; watchdog will recover the saved settings')
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
