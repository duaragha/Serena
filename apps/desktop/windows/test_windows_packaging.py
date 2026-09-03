"""Static checks for the Windows packaging surface.

A real Windows build cannot run on the Linux dev host, so these assert the
things that would otherwise only fail on the windows-latest runner, or worse,
on a user's machine: the sidecar is frozen console=False, so anything wrong
inside it dies without printing.

Deliberately ASCII-only. These run under CI harnesses that capture stdout with
a non-UTF-8 codec, where a single em dash in an assertion message aborts the
run with UnicodeEncodeError instead of reporting the real failure.

    .venv/bin/python -m pytest desktop-electron/windows/test_windows_packaging.py -q
"""

from __future__ import annotations

import ast
import types
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WINDOWS_DIR = Path(__file__).resolve().parent
DESKTOP_DIR = WINDOWS_DIR.parent
REPO_ROOT = DESKTOP_DIR.parent.parent if DESKTOP_DIR.parent.name == "apps" else DESKTOP_DIR.parent

BUILDER_YML = WINDOWS_DIR / "electron-builder.win.yml"
SPEC_FILE = WINDOWS_DIR / "sidecar-win.spec"
BUILD_SCRIPT = WINDOWS_DIR / "build-win.ps1"
ENTRYPOINT = WINDOWS_DIR / "sidecar-win.py"
PACKAGE_JSON = DESKTOP_DIR / "package.json"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "windows-desktop.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "desktop-release.yml"

SIDECAR_NAME = "serena-web-sidecar"


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def builder() -> dict:
    return _load_yaml(BUILDER_YML)


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load_yaml(WORKFLOW)


@pytest.fixture(scope="module")
def spec_calls() -> dict:
    """Execute the spec with stub builders and capture what it declares.

    PyInstaller execs a .spec as plain Python with Analysis/EXE/COLLECT injected
    as globals. Doing the same here checks the real file rather than a
    hand-parsed approximation of it.
    """
    pytest.importorskip(
        "PyInstaller",
        reason="the spec imports PyInstaller.utils.hooks; scripts/build-sidecar.sh "
        "requires it in the repo venv for a real build",
    )
    captured: dict[str, dict] = {}

    def recorder(kind: str):
        def build(*args, **kwargs):
            captured[kind] = {"args": args, "kwargs": kwargs}
            obj = types.SimpleNamespace(**kwargs)
            obj.pure = []
            obj.scripts = []
            obj.binaries = []
            obj.datas = []
            return obj

        return build

    namespace = {
        "__file__": str(SPEC_FILE),
        "__name__": "__main__",
        "SPECPATH": str(WINDOWS_DIR),
        "DISTPATH": str(DESKTOP_DIR / "build" / "windows-sidecar"),
        "workpath": str(DESKTOP_DIR / "build" / "pyinstaller-windows"),
        "Analysis": recorder("Analysis"),
        "PYZ": recorder("PYZ"),
        "EXE": recorder("EXE"),
        "COLLECT": recorder("COLLECT"),
    }
    exec(compile(SPEC_FILE.read_text(encoding="utf-8"), str(SPEC_FILE), "exec"), namespace)
    return captured


# -- the spec ---------------------------------------------------------------


def test_spec_builds_onedir_windowless(spec_calls):
    exe = spec_calls["EXE"]["kwargs"]
    assert exe["console"] is False, "a console window must not flash behind the Electron UI"
    # COLLECT present + exclude_binaries is what makes this onedir rather than
    # onefile. onefile would unpack to %TEMP% on every launch and defeat the
    # differential NSIS updates electron-builder generates.
    assert exe["exclude_binaries"] is True
    assert "COLLECT" in spec_calls
    assert exe["name"] == SIDECAR_NAME
    assert spec_calls["COLLECT"]["kwargs"]["name"] == SIDECAR_NAME


def test_spec_freezes_the_windows_entrypoint(spec_calls):
    scripts = spec_calls["Analysis"]["args"][0]
    assert [Path(s).name for s in scripts] == ["sidecar-win.py"]
    assert all(Path(s).is_file() for s in scripts)


def test_spec_carries_the_imports_pyinstaller_cannot_trace(spec_calls):
    hidden = spec_calls["Analysis"]["kwargs"]["hiddenimports"]
    # voice/call/__init__.py resolves its runtime exports through
    # import_module() inside a module __getattr__, which the static graph
    # cannot follow, and ui.web imports them at module scope.
    assert "voice.call.orchestrator" in hidden
    assert "ui.web" in hidden
    # flask-sock's transport, which the terminal WebSocket upgrade needs.
    assert "simple_websocket" in hidden


def test_spec_excludes_the_linux_desktop_stack(spec_calls):
    excludes = spec_calls["Analysis"]["kwargs"]["excludes"]
    for module in ("gi", "vte", "cairo", "desktop.app_gtk"):
        assert module in excludes, f"{module} is GTK/VTE and must not ship on Windows"
    for module in ("voice.desktop", "voice.desk.client", "pulsectl"):
        assert module in excludes, f"{module} is a Linux desk runtime"


def test_spec_keeps_numpy_whole(spec_calls):
    """numpy must be collected, not merely named.

    ui.web imports voice.call at boot and the call pipeline resamples with
    numpy. Listing it as a bare hidden import bundles a copy without its
    compiled extensions, which fails at runtime on numpy._core.
    """
    excludes = spec_calls["Analysis"]["kwargs"]["excludes"]
    assert "numpy" not in excludes
    hidden = spec_calls["Analysis"]["kwargs"]["hiddenimports"]
    # numpy._core is the exact module the hollow-copy failure died on, and
    # collect_all's hiddenimports is what pulls it in. Note that the binaries
    # list stays empty on purpose: collect_dynamic_libs skips Python extension
    # modules, so numpy's .so/.pyd files arrive through the module graph via
    # these names rather than as loose binaries.
    assert "numpy._core" in hidden
    assert len([h for h in hidden if h.startswith("numpy.")]) > 100, (
        "numpy looks named rather than collected"
    )
    datas = spec_calls["Analysis"]["kwargs"]["datas"]
    assert any("numpy" in str(dest) for _src, dest in datas), "collect_all must contribute numpy data"


def test_spec_bundles_the_legacy_winpty_agent(spec_calls):
    """The frozen host's stable backend is unusable without its helper exe."""
    import sys
    if sys.platform != "win32":
        pytest.skip("winpty is only available to collect_all on Windows")
    datas = spec_calls["Analysis"]["kwargs"]["datas"]
    assert any(
        Path(source).name == "winpty-agent.exe" and str(destination).replace("\\", "/") == "winpty"
        for source, destination in datas
    ), "the frozen sidecar must carry winpty-agent.exe"


# -- electron-builder config ------------------------------------------------


def test_builder_targets_nsis_x64(builder):
    targets = builder["win"]["target"]
    assert any(t["target"] == "nsis" and "x64" in t["arch"] for t in targets)
    assert builder["appId"]
    assert "${version}" in builder["artifactName"]
    assert "${ext}" in builder["artifactName"]


def test_builder_publishes_to_github_for_electron_updater(builder):
    feed = builder["publish"][0]
    assert feed["provider"] == "github"
    assert feed["owner"] and feed["repo"]
    # Differential updates need the .blockmap electron-builder emits alongside
    # the installer; without it every update is a full download.
    assert builder["nsis"]["differentialPackage"] is True


def test_windows_and_linux_publish_to_one_feed(builder):
    """The comment in electron-builder.win.yml promises this; hold it to it.

    --config makes electron-builder ignore package.json's build block entirely,
    so the feed is repeated rather than inherited. If the two drift, one
    platform stops seeing updates and nothing fails loudly.
    """
    package = __import__("json").loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    assert builder["publish"] == package["build"]["publish"]


def test_builder_ships_the_frozen_sidecar_where_the_build_puts_it(builder):
    resource = builder["extraResources"][0]
    # Paths in an electron-builder config resolve against the project dir
    # (desktop-electron/), which is also where build-win.ps1 writes the freeze.
    assert resource["from"] == f"build/windows-sidecar/{SIDECAR_NAME}"
    assert resource["to"] == "sidecar"


def test_builder_packs_the_windows_entry_it_declares(builder):
    """extraMetadata rewrites `main`, so that file has to be in `files`."""
    main = builder["extraMetadata"]["main"]
    assert main in builder["files"]
    assert (DESKTOP_DIR / main).is_file()
    # main-win.js reaches these with require('../...'); missing any of them
    # breaks the packaged app at launch, not at build time.
    for required in ("main.js", "updates.js"):
        assert required in builder["files"]


# -- build script -----------------------------------------------------------


def test_build_script_matches_the_spec_output_path():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "build\\windows-sidecar" in script
    assert f"{SIDECAR_NAME}\\{SIDECAR_NAME}.exe" in script
    assert "sidecar-win.spec" in script
    assert "electron-builder" in script
    # The freeze has to happen before packaging, since electron-builder copies
    # its output in as extraResources.
    assert script.index("PyInstaller") < script.index("electron-builder")


def test_build_script_boots_the_frozen_sidecar():
    """Regression guard.

    The build used to stop at Test-Path on the exe. Because the sidecar is
    console=False, an untraced import produced a binary that built cleanly,
    started, died silently, and left Electron waiting on a port that never
    opened. The build must actually run it and poll the health probe.
    """
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "/api/health" in script, "the build must poll the readiness probe"
    assert "Start-Process" in script
    assert "HasExited" in script, "a sidecar that exits early must fail the build"
    assert "--pty-smoke" in script, "the build must exercise the packaged PTY backend"


def test_build_script_defaults_to_not_publishing():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert '"always" } else { "never" }' in script


# -- the sidecar entry point ------------------------------------------------


def test_entrypoint_repairs_detached_console_streams_before_importing_flask():
    """A windowless executable must not let Flask write to an invalid handle."""
    source = ENTRYPOINT.read_text(encoding="utf-8")

    assert "def _repair_standard_streams" in source
    assert "os.fstat(stream.fileno())" in source
    assert "sink = open(" in source
    assert "os.devnull" in source
    assert source.index("_repair_standard_streams()") < source.index(
        'import_module("ui.web")'
    )


def test_entrypoint_does_not_redeclare_routes_ui_web_owns():
    """Regression guard.

    /api/health lives in ui.web as `api_health` so mobile_host serves it too.
    This entry point used to declare a second view on the identical rule; since
    ui.web registers first its endpoint always won, leaving unreachable code
    that read as though it were the live probe.
    """
    tree = ast.parse(ENTRYPOINT.read_text(encoding="utf-8"), filename=str(ENTRYPOINT))
    routed = [
        decorator
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and isinstance(decorator.func.value, ast.Name)
        and decorator.func.value.id == "app"
    ]
    assert routed == [], "the Windows entry point must not add Flask routes"


def test_entrypoint_binds_loopback_only():
    source = ENTRYPOINT.read_text(encoding="utf-8")
    assert "the sidecar may only bind to loopback" in source
    assert 'os.environ.setdefault("SERENA_CALL_RUNTIME", "lazy")' in source


def test_entrypoint_exposes_a_packaged_pty_smoke_mode():
    source = ENTRYPOINT.read_text(encoding="utf-8")
    assert 'parser.add_argument("--pty-smoke", action="store_true")' in source
    assert "SERENA_PTY_OK" in source
    assert '"serena command shim.cmd"' in source
    assert "pty_terminal.spawn(" in source


# -- CI ---------------------------------------------------------------------


def test_validation_workflow_never_publishes(workflow):
    """Only desktop-release.yml may publish.

    Both workflows would upload into the same GitHub Release for a tag, and the
    loser of that race gets a 422 on a tag that already exists. desktop-release
    serialises Linux then Windows for exactly this reason.
    """
    triggers = workflow.get("on", workflow.get(True))
    assert "tags" not in (triggers.get("push") or {}), "tags belong to desktop-release.yml"
    assert workflow["permissions"]["contents"] == "read"
    # Inspect what the steps actually execute. Matching raw file text would
    # also match the comments explaining why publishing is absent.
    for step in workflow["jobs"]["build"]["steps"]:
        command = step.get("run", "")
        assert "--publish" not in command
        assert "-Publish" not in command
        assert "GH_TOKEN" not in (step.get("env") or {})


def test_validation_workflow_builds_on_the_real_runner(workflow):
    job = workflow["jobs"]["build"]
    assert job["runs-on"] == "windows-latest"
    assert isinstance(job["timeout-minutes"], int)
    body = WORKFLOW.read_text(encoding="utf-8")
    assert "build-win.ps1" in body, "validation must run the same script the release runs"


def test_validation_workflow_runs_when_the_frozen_surface_changes(workflow):
    triggers = workflow.get("on", workflow.get(True))
    paths = triggers["pull_request"]["paths"]
    # A change to any of these can break the freeze, and the failure must not
    # wait for a release tag.
    expected_desktop = "apps/desktop/windows/**" if "apps/desktop/windows/**" in paths else "desktop-electron/windows/**"
    for required in (expected_desktop, "ui/**", "core/**", "requirements-windows.txt"):
        assert required in paths
    # GitHub does not expand YAML anchors, so the push filter has to repeat the
    # list rather than alias it. Assert it really did.
    assert triggers["push"]["paths"] == paths


def test_release_workflow_still_owns_windows_publishing():
    """The deliverable this repo actually releases from must keep working."""
    release = _load_yaml(RELEASE_WORKFLOW)
    triggers = release.get("on", release.get(True))
    assert triggers["push"]["tags"] == ["v*"]
    windows = release["jobs"]["windows"]
    assert windows["runs-on"] == "windows-latest"
    # Windows waits for Linux so the two never race to create the release.
    assert windows["needs"] == "linux"
    assert release["permissions"]["contents"] == "write"
