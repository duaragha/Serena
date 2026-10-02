# Windows PC game mode

The standalone watcher identifies running games and gives the VMs less CPU time
while they run. It restores the previous CPU limits and process priorities 20
seconds after the last game exits, including a game crash. It never shuts down or
pauses a VM, stops a container, flushes caches, or changes the PC power plan.

The accepted targets are approximately 30% background CPU and 70% background RAM.
These describe the background workload; the game adds its own resource usage.
They are measured targets, not guarantees for arbitrary workloads. The RAM
profile is a separate baseline change requiring one-time offline maintenance.

## Configuration and installation

`scripts/pc_game_mode.py` uses standard-library Python 3.10 or newer on Windows.
The install script registers **Serena PC Game Mode** for the current interactive
user at login. A one-minute repeating watchdog and failure restart recover a
terminated watcher. It runs hidden under `pythonw.exe`. Its source path must
remain present on the synced Projects tree.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_pc_game_mode.ps1
python scripts\pc_game_mode.py status
```

Mutable configuration, logs, status, game classification, and recovery journals
live in `%LOCALAPPDATA%\SerenaGameMode`, outside Git. The installer copies
`config/pc-game-mode.example.json` only if a private configuration does not exist.
Logs rotate at 1 MB with two backups. Every command supports `--config` and
`--state-dir` before its command name.

Default caps are **Docker-Ubuntu: 40% per virtual CPU** and
**BlueBubbles-macOS: 25% per virtual CPU**, with VirtualBox's native `low` priority
scheme. It lowers the protected VM worker's thread priorities using
`controlvm vm-process-priority low`, and restores the original scheme afterward.
On the audited Windows host this changed the actual worker thread base priorities
even though its process-level `PriorityClass` remained Normal. Accessible
VBoxHeadless launchers also receive BelowNormal Windows scheduling priority.
The protected processes include the actual VM workers, not just helper wrappers.
These percentages are not percentages of
the whole PC. With four vCPUs per VM on the audited 12-thread Ryzen 5600X, they
give the guests a nominal budget of 2.6 logical CPUs, plus virtualization and
Windows overhead. The watcher records measured background CPU in `status.json`.
Existing smaller CPU budgets are preserved.

## Core pinning

`vm_affinity_cpus` optionally attempts to confine accessible VBoxHeadless
processes to the highest N logical processors while a game runs. It is disabled
by default (`0`). On the audited VirtualBox 7.2.12 Windows host, hardening denies
affinity changes on the actual CPU-consuming workers; only launcher processes
accept them. Windows CPU Sets changes are denied too. No exclusive game cores
or frame-time improvement is claimed. `affinity_denied_pids` exposes rejected
processes in status instead of reporting successful isolation. CPU caps and the
native priority scheme remain effective without disabling VirtualBox hardening.

Validation refuses a value that would leave fewer than two logical processors
for the game and Windows. A narrower pin that was already in place is preserved
rather than widened, matching how existing smaller CPU budgets are treated.
Masks are journaled and restored exactly like caps and priorities, including
after a crash, and a mask changed externally while a game ran is left alone.
Journals written before this feature existed still load.

The original settings are flushed to an atomic journal **before** changes.
Recovery retains failed restores for another attempt, verifies process creation
times against PID reuse, and preserves settings changed externally. A single
Windows mutex prevents competing controllers. A new watcher restores stale
limits immediately when no game is running, before making discovery requests.

## Game recognition

Oblivion Remastered's two actual executable names are explicitly configured.
Steam discovery reads installed app manifests and caches the official Steam
Store classification. Only `game` apps without software genres are eligible.
Software genres override the API's broad product type, which incorrectly calls
Wallpaper Engine a game. Wallpaper Engine and Steamworks redistributables also
have explicit exclusions. Versioned caches discard earlier classifications.
Unclassified apps are ignored until classification succeeds. Discovery runs at
most every five minutes while no game is running.

Steam games without an explicit rule match running executables under their
installation directory, excluding common launchers, crash reporters, installers,
and other helpers. Explicit filename rules are stricter and override directory
discovery. Add a rule in private `config.json` for a non-Steam game or unusual
launcher. This is not universal game detection: an unusual helper inside an
automatically discovered game directory may need an explicit filename rule.
Steam itself, browsers, fullscreen videos, and executables outside game paths do
not activate the mode. Alt-tabbing keeps the mode active while the game runs.

## RAM profile and rollback

The installed baseline verified on 2026-10-02 is **Docker-Ubuntu: 8192 MiB** and
**BlueBubbles-macOS: 4096 MiB**, with four vCPUs each. Keep this headroom for
Docker's model/build peaks and macOS messaging services. Idle vCPUs do not
consume a full physical core each. Background host RAM was approximately 70%;
that is a measured sample, not a guarantee for future workloads.

VirtualBox memory ballooning does not return RAM to Windows. Shrinking a running
container's memory limit also does not shrink the VirtualBox allocation. Neither
is used to claim host RAM savings. Reference:
[Oracle memory overcommitment](https://docs.oracle.com/en/virtualization/virtualbox/7.2/user/guestadditions.html).

```powershell
python scripts\pc_game_mode.py memory-plan
# Only after both guests have already been shut down gracefully:
python scripts\pc_game_mode.py memory-apply-offline
# Only while both guests are powered off, to restore the saved allocations:
python scripts\pc_game_mode.py memory-restore-offline
```

Offline commands do not stop or start VMs. They validate both guests before any
mutation, save original allocations before changing them, verify each result,
and roll back a partial failure. After a successful resize, memory allocations
remain at the new baseline during normal use as well as gaming. CPU caps toggle
automatically; RAM allocations do not toggle or expand on game exit.

## Verification and disabling

```bash
python -m pytest tests/test_pc_game_mode.py
```

```powershell
# Reversible 30-second live CPU exercise; always attempts restoration:
python scripts\pc_game_mode.py exercise --seconds 30
# Run a finite watcher for acceptance testing before installing the watchdog:
python scripts\pc_game_mode.py watch --max-seconds 60
```

An exercise refuses to run beside an existing watcher. Check guest health before,
during, and afterward; CPU throttling can delay responses under load. No fresh
out-of-memory event or service restart should be accepted as successful proof.

Set `enabled` to `false` in private `config.json` to restore limits at the next
poll and exit, even with a game open. Uninstall disables the live watcher and
waits for an empty restoration journal before removing the scheduled watchdog:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_pc_game_mode.ps1 -Uninstall
```

Do not delete `%LOCALAPPDATA%\SerenaGameMode\restore.json` while it contains saved
settings. The `restore` command can recover it when no watcher is running.
