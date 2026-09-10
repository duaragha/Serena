from core.computer_indicator import ComputerIndicator, _friendly_app, _trim, focus_label


def test_trim_keeps_short_values_and_marks_long_values() -> None:
    assert _trim("  AWS   Console  ", 40) == "AWS Console"
    assert _trim("abcdefgh", 5) == "abcd…"


def test_friendly_app_collapses_duplicate_wm_class_names() -> None:
    assert _friendly_app("microsoft-edge Microsoft-edge") == "Microsoft Edge"
    assert _friendly_app("google-chrome Google-chrome") == "Chrome"


def test_focus_label_includes_app_and_current_tab() -> None:
    session = {
        "target": "display:DisplayPort-1",
        "focused_window": {
            "app": "microsoft-edge Microsoft-edge",
            "title": "IAM Identity Center | us-east-2 - Work - Microsoft Edge",
        },
    }

    assert focus_label(session) == (
        "Microsoft Edge · IAM Identity Center | us-east-2 - Work - Microsoft Edge"
    )


def test_focus_label_falls_back_to_scope_without_window_details() -> None:
    assert focus_label({"target": "display:DisplayPort-1"}) == (
        "display:DisplayPort-1 · waiting for window details"
    )


def test_state_details_surfaces_draft_and_model_timing() -> None:
    indicator = ComputerIndicator.__new__(ComputerIndicator)
    details = indicator._state_details(
        {
            "driver": "astra",
            "mode": "watch",
            "observation_state": "thinking",
            "inspection_started_at": 0,
            "observation": "",
            "observation_preview": "click Add user",
            "last_model_ms": 2430,
        }
    )

    assert details[1:5] == ("watching", "thinking", "THINKING", "#ffc56d")
    assert details[5] == "draft · click Add user"
    assert details[6] == "last check 2.4s"
