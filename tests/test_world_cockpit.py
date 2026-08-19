from __future__ import annotations

import json

from core.state_graph import StateGraphStore
from core.world_cockpit import FixtureAdapter, JsonURLAdapter, WorldCockpit


def _cockpit(tmp_path) -> WorldCockpit:
    return WorldCockpit(StateGraphStore(tmp_path / "graph.sqlite3"))


def test_all_world_kinds_normalize_into_one_graph_event_stream(tmp_path) -> None:
    cockpit = _cockpit(tmp_path)
    fixtures = [
        FixtureAdapter(
            "calendar-local",
            "event",
            [
                {
                    "title": "Therapy",
                    "summary": "Appointment",
                    "start_at": "2026-08-08T10:00:00",
                    "end_at": "2026-08-08T11:00:00",
                    "timezone": "America/Toronto",
                    "location": "Office",
                    "confidence": 1.0,
                }
            ],
            fetched_at=100,
            freshness_seconds=300,
        ),
        FixtureAdapter(
            "weather-local",
            "weather",
            [
                {
                    "title": "Current weather",
                    "summary": "Clear, 24 C",
                    "location": {
                        "label": "Toronto",
                        "lat": 43.6532,
                        "lon": -79.3832,
                        "timezone": "America/Toronto",
                    },
                }
            ],
            fetched_at=100,
        ),
        FixtureAdapter(
            "house-local",
            "household",
            [{"title": "Front door", "summary": "Closed", "online": True}],
            fetched_at=100,
        ),
        FixtureAdapter(
            "news-local",
            "news",
            [{"title": "Transit update", "summary": "Service resumed"}],
            fetched_at=100,
        ),
    ]

    for adapter in fixtures:
        assert cockpit.refresh(adapter, now=100).state == "available"

    items = cockpit.items(now=101)
    assert {item["world_kind"] for item in items} == {
        "event",
        "weather",
        "household",
        "news",
    }
    event = next(item for item in items if item["world_kind"] == "event")
    assert event["start_at"] == "2026-08-08T14:00:00Z"
    assert event["end_at"] == "2026-08-08T15:00:00Z"
    assert any(
        graph_event.event_type == "entity.upserted" for graph_event in cockpit.graph.events()
    )


def test_cross_source_deduplication_merges_evidence(tmp_path) -> None:
    cockpit = _cockpit(tmp_path)
    first = FixtureAdapter(
        "calendar-a",
        "event",
        [
            {
                "title": "Dentist",
                "start_at": "2026-08-09T09:00:00Z",
                "dedupe_key": "dentist-2026-08-09",
                "source_id": "a-1",
                "confidence": 0.7,
            }
        ],
        fetched_at=100,
    )
    second = FixtureAdapter(
        "calendar-b",
        "event",
        [
            {
                "title": "Dentist",
                "start_at": "2026-08-09T09:00:00Z",
                "dedupe_key": "dentist-2026-08-09",
                "source_id": "b-9",
                "confidence": 0.95,
            }
        ],
        fetched_at=101,
    )

    cockpit.refresh(first, now=100)
    cockpit.refresh(second, now=101)
    event_count = len(cockpit.graph.events(limit=2_000))
    cockpit.refresh(second, now=101)

    items = cockpit.items(now=102)
    assert len(cockpit.graph.events(limit=2_000)) == event_count
    assert len(items) == 1
    assert items[0]["confidence"] == 0.95
    assert {entry["provider"] for entry in items[0]["evidence"]} == {
        "calendar-a",
        "calendar-b",
    }


def test_network_failure_uses_cache_then_reports_explicit_unavailable(tmp_path) -> None:
    cockpit = _cockpit(tmp_path)
    success = FixtureAdapter(
        "weather",
        "weather",
        [{"title": "Weather", "summary": "Rain"}],
        fetched_at=100,
        freshness_seconds=10,
    )
    assert cockpit.refresh(success, now=100).state == "available"

    failed = FixtureAdapter(
        "weather",
        "weather",
        [],
        error=TimeoutError("provider timed out"),
    )
    result = cockpit.refresh(failed, now=120)
    assert result.state == "stale"
    assert result.cached is True
    assert "TimeoutError" in result.reason
    assert cockpit.provider_states(now=120)[0]["state"] == "stale"
    assert cockpit.items(now=120)[0]["freshness"] == "stale"

    other = FixtureAdapter(
        "news",
        "news",
        [],
        error=OSError("offline"),
    )
    unavailable = cockpit.refresh(other, now=120)
    assert unavailable.state == "unavailable"
    states = {item["provider"]: item["state"] for item in cockpit.provider_states(now=120)}
    assert states == {"news": "unavailable", "weather": "stale"}


def test_snapshot_is_maplibre_compatible_and_has_evidence_and_voice_handoff(tmp_path) -> None:
    cockpit = _cockpit(tmp_path)
    cockpit.refresh(
        FixtureAdapter(
            "weather",
            "weather",
            [
                {
                    "title": "Weather warning",
                    "summary": "Heavy rain",
                    "location": {"label": "Toronto", "lat": 43.65, "lon": -79.38},
                    "confidence": 0.9,
                    "relevance": 1.0,
                }
            ],
            fetched_at=100,
        ),
        now=100,
    )

    snapshot = cockpit.snapshot(now=101)

    assert snapshot["map_style"]["version"] == 8
    source = snapshot["map_style"]["sources"]["cockpit"]
    assert source["type"] == "geojson"
    assert source["data"]["features"][0]["geometry"]["coordinates"] == [
        -79.38,
        43.65,
    ]
    assert snapshot["cards"][0]["evidence"][0]["provider"] == "weather"
    assert "Weather warning" in snapshot["voice_handoff"]
    assert "confidence 0.90" in snapshot["voice_handoff"]


def test_json_url_adapter_passes_timeout_and_never_needs_a_paid_dependency(tmp_path) -> None:
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps({"records": [{"title": "Local event"}]}).encode()

    def opener(request, *, timeout):
        calls.append((request.full_url, timeout))
        return Response()

    adapter = JsonURLAdapter(
        provider="local-feed",
        kind="event",
        url="http://127.0.0.1:8080/events?api_key=fixture-secret",
        opener=opener,
    )
    result = adapter.fetch(2.5)

    assert calls == [("http://127.0.0.1:8080/events?api_key=fixture-secret", 2.5)]
    assert result.records == ({"title": "Local event"},)
    assert result.source_url == "http://127.0.0.1:8080/events?api_key=fixture-secret"

    cockpit = _cockpit(tmp_path)
    cockpit.refresh(adapter)
    evidence = cockpit.items()[0]["evidence"][0]
    assert evidence["source_url"] == "http://127.0.0.1:8080/events"
    assert "fixture-secret" not in json.dumps(cockpit.snapshot())


def test_relevance_uses_the_refresh_clock_instead_of_wall_clock(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("core.world_cockpit.time.time", lambda: 10_000_000)
    cockpit = _cockpit(tmp_path)
    cockpit.refresh(
        FixtureAdapter(
            "fixture-news",
            "news",
            [{"title": "Deterministic ranking", "confidence": 0.8}],
            fetched_at=1_000,
        ),
        now=1_000,
    )

    assert cockpit.items(now=1_001)[0]["relevance"] == 0.65


def test_record_urls_are_sanitized_before_persistence_and_cards(tmp_path) -> None:
    cockpit = _cockpit(tmp_path)
    cockpit.refresh(
        FixtureAdapter(
            "fixture-news",
            "news",
            [
                {
                    "title": "Secret-free evidence",
                    "url": "https://news.example/story?id=7&api_key=top-secret#private",
                }
            ],
            fetched_at=100,
        ),
        now=100,
    )

    snapshot = cockpit.snapshot(now=101)
    assert cockpit.items(now=101)[0]["url"] == "https://news.example/story"
    assert snapshot["cards"][0]["url"] == "https://news.example/story"
    encoded = json.dumps(snapshot)
    provider = cockpit.graph.entity("service:cockpit:fixture-news")
    assert provider is not None
    persisted = json.dumps(provider.attributes)
    assert "top-secret" not in encoded
    assert "api_key" not in encoded
    assert "top-secret" not in persisted
    assert "api_key" not in persisted
