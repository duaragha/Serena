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

    def test_a_night_out_belongs_to_the_day_it_started(self, db):
        """His day runs to 5am: getting home at 1am is the end of Sunday."""

        location.store(location.validate({"points": [
            _visit(arrived=_at(22, 0, day=20), departed=_at(1, 0, day=21))]}))
        assert location.visits_on(date(2026, 9, 20))
        assert not location.visits_on(date(2026, 9, 21))

    def test_a_lost_departure_is_read_off_where_he_went_next(self, db):
        """The phone died at a friend's; he turned up at home at 12:27am."""

        location.store(location.validate({"points": [
            {"id": "v1", "kind": "visit", "lat": 43.696, "lng": -79.8008, "at": _at(22, 19),
             "arrived": _at(22, 19)},
            {"id": "v2", "kind": "visit", "lat": 43.6857, "lng": -79.8013, "at": _at(0, 27, day=21),
             "arrived": _at(0, 27, day=21), "departed": _at(7, 20, day=21)}]}))
        first = location.visits_on(date(2026, 9, 20))[0]
        assert first.departed == _at(0, 27, day=21) and first.departed_inferred

    def test_a_fix_far_away_is_an_earlier_departure_than_the_next_visit(self, db):
        location.store(location.validate({"points": [
            {"id": "v1", "kind": "visit", "lat": 43.70, "lng": -79.80, "at": _at(20), "arrived": _at(20)},
            {"id": "s1", "kind": "significant", "lat": 43.75, "lng": -79.80, "at": _at(21)},
            {"id": "v2", "kind": "visit", "lat": 43.80, "lng": -79.80, "at": _at(23), "arrived": _at(23)}]}))
        assert location.visits_on(date(2026, 9, 20))[0].departed == _at(21)

    def test_nothing_is_inferred_across_a_day_with_the_phone_off(self, db):
        location.store(location.validate({"points": [
            {"id": "v1", "kind": "visit", "lat": 43.70, "lng": -79.80, "at": _at(20), "arrived": _at(20)},
            {"id": "v2", "kind": "visit", "lat": 43.80, "lng": -79.80, "at": _at(20, day=22),
             "arrived": _at(20, day=22)}]}))
        first = location.visits_on(date(2026, 9, 20))[0]
        assert first.departed is None and not first.departed_inferred


class TestHomeAndWork:
    """Never ask him where home or work is."""

    def _week(self):
        points = []
        # Home: nights. Work: weekday 9-5, 27 km away. Sep 21 2026 is a Monday.
        for d in (21, 22):
            points.append({"id": f"home-{d}", "kind": "visit", "lat": 43.6857, "lng": -79.8013,
                           "at": _at(18, 5, day=d - 1), "arrived": _at(18, 5, day=d - 1),
                           "departed": _at(7, 15, day=d)})
            points.append({"id": f"work-{d}", "kind": "visit", "lat": 43.718, "lng": -79.4691,
                           "at": _at(9, 17, day=d), "arrived": _at(9, 17, day=d),
                           "departed": _at(17, 9, day=d)})
        points.append({"id": "cafe", "kind": "visit", "lat": 43.60, "lng": -79.64,
                       "at": _at(14, day=20), "arrived": _at(14, day=20), "departed": _at(17, day=20)})
        location.store(location.validate({"points": points}))

    def test_home_is_where_he_sleeps_and_work_is_weekday_daytime(self, db):
        from core.journal import places

        self._week()
        found = places.anchors(now=_at(12, day=22))
        assert found["home"] == (43.6857, -79.8013)
        assert found["work"] == (43.718, -79.4691)

    def test_a_weekend_afternoon_somewhere_is_not_work(self, db):
        from core.journal import places

        self._week()
        assert places.anchor_for(43.60, -79.64, now=_at(12, day=22)) == ""

    def test_nothing_is_guessed_without_the_history_to_show_it(self, db):
        from core.journal import places

        location.store(location.validate({"points": [_visit()]}))
        assert places.anchors(now=_at(20)) == {}

    def test_a_name_he_gave_beats_home(self, db, tmp_path, monkeypatch):
        from core.journal import store

        monkeypatch.setenv("SERENA_JOURNAL_DB", str(tmp_path / "journal.sqlite3"))
        self._week()
        assert store.place_for(43.6857, -79.8013) == "home"
        store.name_place("mom and dad's", 43.6857, -79.8013)
        assert store.place_for(43.6857, -79.8013) == "mom and dad's"


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
