import io
import json

import pytest

from scripts import pc_game_mode as game


def configuration():
    return {
        'vboxmanage': r'C:\VBox\VBoxManage.exe', 'enabled': True,
        'poll_seconds': 5, 'cooldown_seconds': 20,
        'targets': {'background_cpu_pct': 30, 'background_ram_pct': 70},
        'vms': [{'name': 'Docker', 'cpu_cap': 50, 'memory_mib': 8192},
                {'name': 'Mac', 'cpu_cap': 25, 'memory_mib': 4096}],
        'games': [{'name': 'Oblivion', 'root': r'C:\Games\Oblivion',
                   'executables': ['OblivionRemastered.exe', 'OblivionRemastered-Win64-Shipping.exe']}],
    }


class FakeVBox:
    executable = r'C:\VBox\VBoxManage.exe'

    def __init__(self):
        self.machines = {
            'Docker': {'UUID': 'docker-id', 'VMState': 'running', 'memory': '12288', 'cpuexecutioncap': '85'},
            'Mac': {'UUID': 'mac-id', 'VMState': 'running', 'memory': '6144', 'cpuexecutioncap': '90'},
        }
        self.fail = None
        self.calls = []

    def info(self, name):
        return next(value.copy() for key, value in self.machines.items()
                    if name in (key, value['UUID']))

    def cap(self, name, cap, state):
        if self.fail == name:
            self.fail = None
            raise RuntimeError('temporary VM failure')
        self.calls.append((name, cap, state))
        value = next(v for k, v in self.machines.items() if name in (k, v['UUID']))
        value['cpuexecutioncap'] = str(cap)

    def call(self, action, name, option, value):
        assert action == 'modifyvm' and option == '--memory'
        if self.fail == name:
            self.fail = None
            raise RuntimeError('temporary memory failure')
        self.calls.append((action, name, option, value))
        row = next(v for k, v in self.machines.items() if name in (k, v['UUID']))
        row['memory'] = str(value)


class FakeWindows:
    def __init__(self):
        self.values = {10: (100, game.NORMAL_PRIORITY)}
        self.calls = []

    def priority(self, process, value=None):
        row = self.values.get(process.pid)
        if row is None or row[0] != process.created:
            return None
        if value is not None:
            self.values[process.pid] = (row[0], value)
            self.calls.append((process.pid, value))
        return self.values[process.pid][1]


@pytest.fixture
def environment(tmp_path):
    config, vbox, windows = configuration(), FakeVBox(), FakeWindows()
    controller = game.Controller(config, vbox, windows, tmp_path)
    processes = [game.Process(10, r'C:\VBox\VBoxHeadless.exe', 100)]
    return config, vbox, windows, controller, processes


@pytest.mark.parametrize('path,expected', [
    (r'c:\games\OBLIVION\OblivionRemastered.exe', ['Oblivion']),
    (r'C:\Games\Oblivion\Binaries\OblivionRemastered-Win64-Shipping.exe', ['Oblivion']),
    (r'C:\Games\OblivionBackup\OblivionRemastered.exe', []),
    (r'C:\Program Files\Steam\steam.exe', []),
    (r'C:\Games\wallpaper_engine\wallpaper64.exe', []),
    (r'C:\Program Files\Browser\chrome.exe', []),
    (r'C:\Games\Oblivion\Engine\CrashReportClient.exe', []),
    (r'C:\Games\Oblivion\obse64_loader.exe', []),
])
def test_only_game_executables_activate(path, expected):
    assert game.game_names([game.Process(1, path, 10)], configuration()['games']) == expected


def test_last_game_exit_and_cooldown_restore_exact_settings(environment):
    config, vbox, windows, controller, processes = environment
    assert controller.tick(['Oblivion', 'Another Game'], processes, 100)
    assert vbox.info('Docker')['cpuexecutioncap'] == '50'
    assert windows.values[10][1] == game.BELOW_NORMAL
    assert controller.tick(['Another Game'], processes, 110)
    assert controller.tick([], processes, 129)
    assert not controller.tick([], processes, 130)
    assert vbox.info('Docker')['cpuexecutioncap'] == '85'
    assert vbox.info('Mac')['cpuexecutioncap'] == '90'
    assert windows.values[10][1] == game.NORMAL_PRIORITY
    assert vbox.info('Docker')['memory'] == '12288'


def test_crashed_watcher_recovers_without_waiting_cooldown(environment, tmp_path):
    config, vbox, windows, controller, processes = environment
    controller.tick(['Oblivion'], processes, 100)
    restarted = game.Controller(config, vbox, windows, tmp_path)
    assert not restarted.tick([], processes, 101)
    assert vbox.info('Docker')['cpuexecutioncap'] == '85'


def test_restarting_watcher_during_game_preserves_original_snapshot(environment, tmp_path):
    config, vbox, windows, controller, processes = environment
    controller.tick(['Oblivion'], processes, 100)
    restarted = game.Controller(config, vbox, windows, tmp_path)
    restarted.tick(['Oblivion'], processes, 101)
    assert restarted.journal['vms']['Docker']['original'] == 85
    restarted.restore()
    assert vbox.info('Docker')['cpuexecutioncap'] == '85'


def test_snapshot_precedes_mutation(environment, tmp_path):
    _, vbox, _, controller, processes = environment
    original = vbox.cap

    def assert_journal(name, value, state):
        persisted = json.loads((tmp_path / 'restore.json').read_text())
        assert persisted['vms']
        return original(name, value, state)

    vbox.cap = assert_journal
    controller.apply(processes)
    controller.restore()


def test_partial_application_failure_rolls_back_other_vm(environment):
    _, vbox, windows, controller, processes = environment
    vbox.fail = 'mac-id'
    with pytest.raises(RuntimeError, match='temporary'):
        controller.apply(processes)
    assert vbox.info('Docker')['cpuexecutioncap'] == '85'
    assert vbox.info('Mac')['cpuexecutioncap'] == '90'
    assert not controller.active
    assert not windows.calls


def test_failed_restore_retains_original_and_retries(environment, tmp_path):
    _, vbox, _, controller, processes = environment
    controller.apply(processes)
    vbox.fail = 'docker-id'
    assert controller.restore()
    assert controller.active
    assert json.loads((tmp_path / 'restore.json').read_text())['vms']['Docker']['original'] == 85
    assert controller.restore() == []
    assert not controller.active


def test_pid_reuse_does_not_change_unrelated_process_priority(environment):
    _, _, windows, controller, processes = environment
    controller.apply(processes)
    windows.values[10] = (999, 0x80)
    controller.restore()
    assert windows.values[10] == (999, 0x80)


def test_external_setting_changes_are_preserved(environment):
    _, vbox, windows, controller, processes = environment
    controller.apply(processes)
    vbox.machines['Docker']['cpuexecutioncap'] = '60'
    windows.values[10] = (100, 0x80)
    controller.restore()
    assert vbox.info('Docker')['cpuexecutioncap'] == '60'
    assert windows.values[10][1] == 0x80


def test_disabled_mode_immediately_restores_even_while_game_runs(environment):
    config, vbox, _, controller, processes = environment
    controller.tick(['Oblivion'], processes, 100)
    config['enabled'] = False
    assert not controller.tick(['Oblivion'], processes, 101)
    assert vbox.info('Docker')['cpuexecutioncap'] == '85'


def test_vm_started_during_game_receives_cap(environment):
    _, vbox, _, controller, processes = environment
    vbox.machines['Mac']['VMState'] = 'poweroff'
    controller.tick(['Oblivion'], processes, 100)
    assert vbox.info('Mac')['cpuexecutioncap'] == '90'
    vbox.machines['Mac']['VMState'] = 'running'
    controller.tick(['Oblivion'], processes, 105)
    assert vbox.info('Mac')['cpuexecutioncap'] == '25'
    controller.restore()


def test_existing_lower_cpu_and_priority_limits_are_not_increased(environment):
    _, vbox, windows, controller, processes = environment
    vbox.machines['Docker']['cpuexecutioncap'] = '30'
    windows.values[10] = (100, 0x40)
    controller.apply(processes)
    assert vbox.info('Docker')['cpuexecutioncap'] == '30'
    assert windows.values[10][1] == 0x40
    controller.restore()


def test_protected_helper_does_not_prevent_vm_cpu_limits(environment):
    _, vbox, windows, controller, processes = environment
    original_priority = windows.priority

    def protected(process, value=None):
        return None if value is not None else original_priority(process)

    windows.priority = protected
    controller.apply(processes)
    assert vbox.info('Docker')['cpuexecutioncap'] == '50'
    assert not controller.journal['priorities']
    assert windows.values[10][1] == game.NORMAL_PRIORITY
    controller.restore()
    assert vbox.info('Docker')['cpuexecutioncap'] == '85'


def test_memory_plan_is_read_only_and_labels_estimate(environment):
    config, vbox, _, _, _ = environment
    result = game.memory_plan(config, vbox, {'total_ram_gib': 32, 'total_ram_used_pct': 85})
    assert result['estimated_ram_freed_gib'] == 6
    assert result['estimated_background_ram_used_pct'] == 66.2
    assert result['estimate_only']
    assert not vbox.calls


def test_ram_changes_refuse_all_mutation_when_either_vm_is_online(environment, tmp_path):
    config, vbox, _, _, _ = environment
    vbox.machines['Docker']['VMState'] = 'poweroff'
    with pytest.raises(RuntimeError, match='already be gracefully powered off'):
        game.apply_memory_offline(config, vbox, tmp_path)
    assert not vbox.calls
    assert not (tmp_path / 'memory-original.json').exists()


def test_offline_ram_apply_and_rollback_preserve_original_allocations(environment, tmp_path):
    config, vbox, _, _, _ = environment
    for vm in vbox.machines.values():
        vm['VMState'] = 'poweroff'
    game.apply_memory_offline(config, vbox, tmp_path)
    assert vbox.info('Docker')['memory'] == '8192'
    assert vbox.info('Mac')['memory'] == '4096'
    game.apply_memory_offline(config, vbox, tmp_path)  # Reapplication must not lose originals.
    game.apply_memory_offline(config, vbox, tmp_path, restore=True)
    assert vbox.info('Docker')['memory'] == '12288'
    assert vbox.info('Mac')['memory'] == '6144'


def test_failed_ram_apply_rolls_back_the_first_guest(environment, tmp_path):
    config, vbox, _, _, _ = environment
    for vm in vbox.machines.values():
        vm['VMState'] = 'poweroff'
    vbox.fail = 'mac-id'
    with pytest.raises(RuntimeError, match='temporary memory'):
        game.apply_memory_offline(config, vbox, tmp_path)
    assert vbox.info('Docker')['memory'] == '12288'
    assert vbox.info('Mac')['memory'] == '6144'


def test_steam_tools_are_excluded_and_unclassified_apps_fail_closed(tmp_path):
    steamapps = tmp_path / 'steamapps'
    steamapps.mkdir()
    kinds = {'1': 'game', '2': 'application', '3': 'tool'}
    for app_id, name in [('1', 'Game'), ('2', 'Wallpaper Engine'), ('3', 'Steamworks'), ('4', 'Offline')]:
        (steamapps / f'appmanifest_{app_id}.acf').write_text(
            f'"AppState" {{ "appid" "{app_id}" "name" "{name}" "installdir" "{name}" }}')
        (steamapps / 'common' / name).mkdir(parents=True)

    def classify(app_id):
        if app_id == '4':
            raise OSError('network unavailable')
        return kinds[app_id]

    rules = game.discover_steam([str(tmp_path)], {}, classify)
    assert [rule['name'] for rule in rules] == ['Game']


@pytest.mark.parametrize('genres,expected', [
    ([{'id': '4'}, {'id': '23'}, {'id': '51'}, {'id': '57'}], 'application'),
    ([{'id': '60'}], 'application'),
    ([{'id': '3'}], 'game'),
])
def test_store_software_genres_override_misleading_game_type(monkeypatch, genres, expected):
    response = {'1': {'success': True, 'data': {'type': 'game', 'genres': genres}}}
    monkeypatch.setattr(game.urllib.request, 'urlopen',
                        lambda *args, **kwargs: io.BytesIO(json.dumps(response).encode()))
    assert game.steam_type('1') == expected


def test_wallpaper_engine_is_excluded_even_with_a_stale_game_classification(tmp_path):
    steamapps = tmp_path / 'steamapps'
    steamapps.mkdir()
    (steamapps / 'common' / 'wallpaper_engine').mkdir(parents=True)
    (steamapps / 'appmanifest_431960.acf').write_text(
        '"appid" "431960" "name" "Wallpaper Engine" "installdir" "wallpaper_engine"')
    assert game.discover_steam([str(tmp_path)], {'431960': 'game'}) == []


def test_corrupt_journal_is_not_overwritten(environment, tmp_path):
    config, vbox, windows, _, _ = environment
    (tmp_path / 'restore.json').write_text('{broken')
    with pytest.raises(json.JSONDecodeError):
        game.Controller(config, vbox, windows, tmp_path)
    assert (tmp_path / 'restore.json').read_text() == '{broken'
    assert not vbox.calls


def test_corrupt_game_cache_does_not_block_restoring_cpu_settings(environment, tmp_path):
    config, vbox, windows, controller, processes = environment
    controller.apply(processes)
    config_file = tmp_path / 'config.json'
    config_file.write_text(json.dumps(config))
    (tmp_path / game.STEAM_GAMES_CACHE).write_text('{broken cache')
    game.watch(config_file, tmp_path, windows, vbox, max_seconds=0)
    assert vbox.info('Docker')['cpuexecutioncap'] == '85'
    assert vbox.info('Mac')['cpuexecutioncap'] == '90'
