from pathlib import Path

from core.scanner import scan_sessions


SESSION_ID = "11111111-2222-4333-8444-555555555555"


def _session(project: Path) -> Path:
    project.mkdir(parents=True)
    path = project / f"{SESSION_ID}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return path


def test_resident_brain_is_indexed_but_headless_utilities_stay_hidden(
    tmp_path: Path,
) -> None:
    brain = _session(tmp_path / "-home-raghav--cache-serena-headless-brain")
    _session(tmp_path / "-home-raghav--cache-serena-headless-frontdoor")
    _session(tmp_path / "-home-raghav--cache-serena-headless-title")

    found = list(scan_sessions(tmp_path))

    assert found == [
        ("-home-raghav--cache-serena-headless-brain", brain)
    ]
