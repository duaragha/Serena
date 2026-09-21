"""His location, stored on the PC, reachable only with the phone's own key."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from core.journal import location
from core.webhook_signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, WebhookReplayStore, sign

TZ = location.TZ
PHONE_KEY = "p" * 48
WIDE_KEY = "w" * 48


def _at(hour: int, minute: int = 0, day: int = 20) -> float:
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ).timestamp()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "location.sqlite3"
    monkeypatch.setenv("SERENA_LOCATION_DB", str(path))
    return path


def _visit(point_id="v1", arrived=None, departed=None, place=""):
    return {"id": point_id, "kind": "visit", "lat": 43.7, "lng": -79.4,
            "accuracy": 35, "at": arrived or _at(14, 30),
            "arrived": arrived or _at(14, 30), "departed": departed, "place": place}


class TestVisits:
    def test_the_departure_fills_in_the_visit_it_belongs_to(self, db):
        """iOS reports a visit on arrival and again on departure, same id."""

        location.store(location.validate({"points": [_visit(place="MOTW Cafe")]}))
        location.store(location.validate({"points": [_visit(departed=_at(17, 10))]}))
        [visit] = location.visits_on(date(2026, 9, 20))
        assert visit.departed == _at(17, 10)
        assert visit.place == "MOTW Cafe", "an empty later geocode must not erase the name"
        assert visit.minutes() == 160

    def test_a_visit_spanning_midnight_belongs_to_both_days(self, db):
        location.store(location.validate({"points": [
            _visit(arrived=_at(22, 0, day=20), departed=_at(1, 0, day=21))]}))
        assert location.visits_on(date(2026, 9, 20))
        assert location.visits_on(date(2026, 9, 21))
        assert not location.visits_on(date(2026, 9, 22))

    def test_significant_changes_are_not_counted_as_visits(self, db):
        location.store(location.validate({"points": [
            {"id": "s1", "kind": "significant", "lat": 43.7, "lng": -79.4, "at": _at(12)}]}))
        assert location.visits_on(date(2026, 9, 20)) == []


class TestWhatItRefuses:
    @pytest.mark.parametrize("points", [
        [],
        [{"id": "x", "kind": "teleport", "lat": 1, "lng": 1, "at": _at(1)}],
        [{"id": "x", "kind": "visit", "lat": 91, "lng": 1, "at": _at(1)}],
        [{"id": "x", "kind": "visit", "lat": float("nan"), "lng": 1, "at": _at(1)}],
        [{"id": "", "kind": "visit", "lat": 1, "lng": 1, "at": _at(1)}],
        [{"id": "x", "kind": "visit", "lat": 1, "lng": 1}],
        [{"id": "x", "kind": "visit", "lat": True, "lng": 1, "at": _at(1)}],
    ])
    def test_malformed_points_are_refused(self, points):
        with pytest.raises(location.LocationPayloadError):
            location.validate({"points": points})

    def test_a_flood_is_refused(self):
        many = [_visit(point_id=f"v{i}") for i in range(location.MAX_POINTS_PER_POST + 1)]
        with pytest.raises(location.LocationPayloadError):
            location.validate({"points": many})


class TestItsOwnKey:
    """The phone's key signs for location and nothing else."""

    def _ingress(self, tmp_path, monkeypatch, *, route_key=PHONE_KEY, wide_key=WIDE_KEY):
        (tmp_path / "cfg").mkdir(exist_ok=True)
        monkeypatch.setenv("SERENA_CONFIG_DIR", str(tmp_path / "cfg"))
        if route_key:
            (tmp_path / "cfg" / location.SECRET_FILE_NAME).write_text(route_key)
        from core.webhook_ingress import default_ingress

        return default_ingress(path=tmp_path / "ingress.sqlite3", secret=wide_key,
                               replay_store=WebhookReplayStore(tmp_path / "replay.sqlite3"))

    def _post(self, ingress, route, body, key):
        raw = json.dumps(body).encode()
        signed = sign(raw, key)
        return ingress.handle(route, raw, {
            TIMESTAMP_HEADER: str(signed.timestamp), SIGNATURE_HEADER: signed.signature})

    def test_the_phone_key_stores_location(self, tmp_path, monkeypatch, db):
        ingress = self._ingress(tmp_path, monkeypatch)
        result = self._post(ingress, "location", {"points": [_visit()]}, PHONE_KEY)
        assert result.accepted, result.reason
        assert location.visits_on(date(2026, 9, 20))

    def test_the_phone_key_cannot_sign_anything_else(self, tmp_path, monkeypatch, db):
        ingress = self._ingress(tmp_path, monkeypatch)
        result = self._post(ingress, "task", {"text": "rm -rf"}, PHONE_KEY)
        assert not result.accepted

    def test_the_wide_key_cannot_sign_for_location(self, tmp_path, monkeypatch, db):
        ingress = self._ingress(tmp_path, monkeypatch)
        result = self._post(ingress, "location", {"points": [_visit()]}, WIDE_KEY)
        assert not result.accepted
        assert location.visits_on(date(2026, 9, 20)) == []

    def test_no_route_key_closes_the_route_instead_of_falling_back(self, tmp_path, monkeypatch, db):
        ingress = self._ingress(tmp_path, monkeypatch, route_key="")
        result = self._post(ingress, "location", {"points": [_visit()]}, WIDE_KEY)
        assert not result.accepted
        assert result.status == 503
