from __future__ import annotations

from core.ambient_classify import StuckLatch, classify
from core.ambient_store import AmbientEvent


def event(kind, at, app="", title="", url="", agent_driven=False):
    return AmbientEvent(
        event_id=f"{at}-{app}-{title}-{url}",
        kind=kind,
        app=app,
        title=title,
        url=url,
        agent_driven=agent_driven,
        started_at=at,
        ended_at=at,
    )


def stuck_session():
    """Search rewordings + failing runs + window bouncing inside 12 minutes."""
    events = []
    base = 10_000.0
    queries = ["xlib BadWindow", "xlib BadWindow fix", "python xlib BadWindow close"]
    for index, query in enumerate(queries):
        at = base + index * 120.0
        events.append(event("tab", at, app="microsoft-edge",
                            title=f"{query} - Search",
                            url=f"https://www.google.com/search?q={query.replace(' ', '+')}"))
    for index in range(3):
        at = base + 60.0 + index * 180.0
        events.append(event("window", at, app="org.gnome.Terminal",
                            title=f"pytest tests/ - {index + 1} FAILED"))
    for index in range(8):
        at = base + 30.0 + index * 60.0
        app = "code" if index % 2 == 0 else "org.gnome.Terminal"
        events.append(event("window", at, app=app, title="flapping"))
    return sorted(events, key=lambda item: item.started_at)


def focused_session():
    """One editor, steady progress, a commit at the end."""
    events = []
    base = 20_000.0
    for index in range(4):
        events.append(event("window", base + index * 300.0, app="code",
                            title=f"ambient_store.py — step {index}"))
    events.append(event("vcs", base + 1_300.0, app="serena", title="commit"))
    return events


def test_stuck_session_fires_exactly_once():
    latch = StuckLatch()
    events = stuck_session()
    now = 10_000.0 + 800.0

    assert latch.check(events, now) is True
    assert latch.check(events, now + 60.0) is False
    assert latch.check(events, now + 120.0) is False


def test_classify_marks_stuck_with_two_or_more_signals():
    result = classify(stuck_session(), now=10_000.0 + 800.0)

    assert result.activity_class == "stuck"
    assert len({signal.name for signal in result.signals}) >= 2


def test_focused_session_never_fires():
    latch = StuckLatch()
    events = focused_session()

    assert latch.check(events, now=20_000.0 + 1_400.0) is False
    assert classify(events, now=20_000.0 + 1_400.0).activity_class == "focused"


def test_single_signal_is_not_stuck():
    events = [
        event("window", 30_000.0 + index * 30.0, app="org.gnome.Terminal",
              title=f"pytest - {index + 1} FAILED")
        for index in range(3)
    ]

    result = classify(events, now=30_000.0 + 200.0)

    assert result.activity_class != "stuck"
    assert len({signal.name for signal in result.signals}) == 1


def test_idle_buffer_classifies_idle():
    events = [
        event("window", 40_000.0, app="code", title="a.py"),
        event("idle", 40_100.0),
    ]

    assert classify(events, now=40_200.0).activity_class == "idle"


def test_browsing_session_classifies_browsing():
    events = [
        event("tab", 50_000.0 + index * 60.0, app="microsoft-edge",
              title=f"article {index}", url=f"https://example.com/{index}")
        for index in range(8)
    ]

    assert classify(events, now=50_000.0 + 600.0).activity_class == "browsing"


def test_agent_driven_periods_never_count_as_activity():
    events = [
        event("window", 60_000.0 + index * 30.0,
              app="code" if index % 2 == 0 else "org.gnome.Terminal",
              title=f"pytest - {index + 1} FAILED", agent_driven=True)
        for index in range(8)
    ]

    result = classify(events, now=60_000.0 + 300.0)

    assert result.signals == ()
    assert result.activity_class == "idle"


def shift(events, delta):
    from dataclasses import replace

    return [
        replace(item, started_at=item.started_at + delta, ended_at=item.ended_at + delta)
        for item in events
    ]


def test_latch_resets_after_cooldown_without_stuck():
    latch = StuckLatch(cooldown_seconds=600.0)
    assert latch.check(stuck_session(), now=10_800.0) is True

    assert latch.check(shift(focused_session(), -9_500.0), now=11_000.0) is False
    assert latch.check(shift(stuck_session(), 1_500.0), now=12_000.0) is True


def test_long_dwell_reads_beyond_the_stuck_window():
    from core.ambient_classify import detect_long_dwell

    base = 70_000.0
    events = [
        event("window", base, app="code", title="a.py"),
        event("window", base + 1_560.0, app="code", title="a.py still"),
    ]

    signal = detect_long_dwell(events)

    assert signal is not None
    assert signal.name == "long_dwell"


def test_long_dwell_plus_one_more_signal_is_stuck():
    base = 80_000.0
    events = [
        event("window", base, app="code", title="a.py"),
        event("window", base + 1_560.0, app="code", title="a.py still"),
        event("window", base + 1_600.0, app="org.gnome.Terminal",
              title="pytest - 1 FAILED"),
        event("window", base + 1_640.0, app="org.gnome.Terminal",
              title="pytest - 2 FAILED"),
        event("window", base + 1_680.0, app="org.gnome.Terminal",
              title="pytest - 3 FAILED"),
    ]

    result = classify(events, now=base + 1_700.0)

    assert result.activity_class == "stuck"
    assert {"long_dwell", "failing_runs"} <= {s.name for s in result.signals}
