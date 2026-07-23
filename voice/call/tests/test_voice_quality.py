from __future__ import annotations

from voice.call.voice_quality import apply_selection, choose_candidate


def _result(
    name: str,
    preference: int,
    *,
    first: float = 40,
    realtime: float = 0.2,
    clipped: float = 0,
    ok: bool = True,
):
    return {
        "name": name,
        "backend": "pocket",
        "voice": name,
        "preference": preference,
        "ok": ok,
        "first_pcm_p90_ms": first,
        "realtime_factor_p90": realtime,
        "clipped_sample_ratio": clipped,
    }


def test_preference_applies_only_after_realtime_gates() -> None:
    selected = choose_candidate(
        [
            _result("preferred-but-slow", 0, first=600),
            _result("realtime", 1),
        ]
    )
    assert selected is not None
    assert selected["name"] == "realtime"


def test_no_candidate_means_no_quality_acceptance() -> None:
    assert choose_candidate([_result("clipped", 0, clipped=0.02)]) is None


def test_existing_report_can_be_reselected_after_gate_fix() -> None:
    report = apply_selection(
        {
            "results": [
                {
                    **_result("alba", 0),
                    "backend": "pocket",
                    "voice": "alba",
                }
            ],
            "acceptance_claim": False,
        }
    )
    assert report["selected"] == "alba"
    assert report["selected_voice"] == "alba"
    assert report["acceptance_claim"] is True
