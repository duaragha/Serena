"""Primary-source world ingestion and local cockpit projections.

The cockpit stores provider health, cached fixture/provider results, normalized
world items, and provenance in ``StateGraphStore``. That gives events, weather,
household state, and news one durable event stream with Serena's personal state
instead of creating a second unsynchronized database.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.security_policy import redact_credentials
from core.state_graph import GraphEntity, StateGraphError, StateGraphStore

COCKPIT_KINDS = frozenset({"event", "weather", "household", "news"})
PROVIDER_STATES = frozenset({"available", "stale", "unavailable"})
MAX_PROVIDER_RECORDS = 100
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_CARDS = 200
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_FRESHNESS_SECONDS = 900.0
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class WorldCockpitError(ValueError):
    """An adapter or world record violated the cockpit contract."""


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider: str
    kind: str
    records: tuple[dict[str, Any], ...]
    source_url: str = ""
    fetched_at: float = 0.0
    freshness_seconds: float = DEFAULT_FRESHNESS_SECONDS
    confidence: float = 0.8


@dataclass(frozen=True, slots=True)
class RefreshResult:
    provider: str
    state: str
    ingested: int
    cached: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "state": self.state,
            "ingested": self.ingested,
            "cached": self.cached,
            "reason": self.reason,
        }


class CockpitAdapter(Protocol):
    provider: str
    kind: str

    def fetch(self, timeout_seconds: float) -> ProviderResult: ...


@dataclass(slots=True)
class FixtureAdapter:
    """Deterministic adapter used by tests and local fixture imports."""

    provider: str
    kind: str
    records: Sequence[Mapping[str, Any]]
    source_url: str = "fixture://local"
    fetched_at: float = 0.0
    freshness_seconds: float = DEFAULT_FRESHNESS_SECONDS
    confidence: float = 0.8
    error: Exception | None = None

    def fetch(self, timeout_seconds: float) -> ProviderResult:
        _timeout(timeout_seconds)
        if self.error is not None:
            raise self.error
        return ProviderResult(
            provider=self.provider,
            kind=self.kind,
            records=tuple(dict(record) for record in self.records),
            source_url=self.source_url,
            fetched_at=self.fetched_at,
            freshness_seconds=self.freshness_seconds,
            confidence=self.confidence,
        )


@dataclass(slots=True)
class JsonURLAdapter:
    """Dependency-free JSON adapter for configured free or local providers.

    The configured endpoint must return a JSON list or an object containing a
    ``records`` list. Network calls are never made by tests unless an injected
    opener is supplied. Credentials in URLs are rejected.
    """

    provider: str
    kind: str
    url: str
    freshness_seconds: float = DEFAULT_FRESHNESS_SECONDS
    confidence: float = 0.8
    opener: Callable[..., Any] = urlopen

    @property
    def source_url(self) -> str:
        return self.url

    def fetch(self, timeout_seconds: float) -> ProviderResult:
        timeout = _timeout(timeout_seconds)
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise WorldCockpitError("provider URL must be absolute http or https")
        if parsed.username or parsed.password:
            raise WorldCockpitError("provider credentials must not be embedded in URLs")
        request = Request(
            self.url,
            headers={"Accept": "application/json", "User-Agent": "Serena-Cockpit/1"},
        )
        with self.opener(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise WorldCockpitError("provider response exceeded one megabyte")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorldCockpitError("provider returned invalid JSON") from exc
        records = payload.get("records") if isinstance(payload, dict) else payload
        if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
            raise WorldCockpitError("provider JSON must contain a records list")
        return ProviderResult(
            provider=self.provider,
            kind=self.kind,
            records=tuple(records),
            source_url=self.url,
            fetched_at=time.time(),
            freshness_seconds=self.freshness_seconds,
            confidence=self.confidence,
        )


class WorldCockpit:
    """Normalize provider evidence into graph entities and dashboard cards."""

    def __init__(self, graph: StateGraphStore) -> None:
        self.graph = graph

    def refresh(
        self,
        adapter: CockpitAdapter,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        now: float | None = None,
    ) -> RefreshResult:
        moment = _timestamp(now)
        provider = _provider(adapter.provider)
        kind = _kind(adapter.kind)
        service_id = _provider_id(provider)
        existing = self.graph.entity(service_id)
        try:
            result = adapter.fetch(_timeout(timeout_seconds))
            result = _validated_result(result, expected_provider=provider, expected_kind=kind)
            fetched = result.fetched_at if result.fetched_at > 0 else moment
            source_url = _public_source_url(result.source_url)
            normalized = [
                self._normalize_record(
                    record,
                    provider=provider,
                    kind=kind,
                    source_url=source_url,
                    observed_at=fetched,
                    ranked_at=moment,
                    freshness_seconds=result.freshness_seconds,
                    default_confidence=result.confidence,
                )
                for record in result.records[:MAX_PROVIDER_RECORDS]
            ]
            self._store_provider(
                provider,
                kind=kind,
                state="available",
                source_url=source_url,
                fetched_at=fetched,
                freshness_seconds=result.freshness_seconds,
                cached_records=normalized,
                reason="provider refresh completed",
            )
            for record in normalized:
                self._store_item(record, provider=provider, source_url=source_url)
            return RefreshResult(
                provider=provider,
                state="available",
                ingested=len(normalized),
                cached=False,
                reason="provider refresh completed",
            )
        except (
            OSError,
            TimeoutError,
            TypeError,
            StateGraphError,
            WorldCockpitError,
            ValueError,
        ) as exc:
            cached_records = _cached_records(existing)
            state = "stale" if cached_records else "unavailable"
            source_url = str(
                (existing.attributes.get("source_url") if existing is not None else "") or ""
            )
            fetched_at = float(
                (existing.attributes.get("fetched_at") if existing is not None else 0.0) or 0.0
            )
            freshness = float(
                (
                    existing.attributes.get("freshness_seconds")
                    if existing is not None
                    else DEFAULT_FRESHNESS_SECONDS
                )
                or DEFAULT_FRESHNESS_SECONDS
            )
            reason = _bounded_error(exc)
            self._store_provider(
                provider,
                kind=kind,
                state=state,
                source_url=source_url,
                fetched_at=fetched_at,
                freshness_seconds=freshness,
                cached_records=cached_records,
                reason=reason,
                observed_at=moment,
            )
            return RefreshResult(
                provider=provider,
                state=state,
                ingested=0,
                cached=bool(cached_records),
                reason=reason,
            )

    def items(
        self,
        *,
        kinds: Sequence[str] | None = None,
        include_stale: bool = True,
        now: float | None = None,
        limit: int = MAX_CARDS,
    ) -> list[dict[str, Any]]:
        moment = _timestamp(now)
        wanted = {_kind(kind) for kind in kinds} if kinds else None
        values: list[dict[str, Any]] = []
        for entity in self.graph.entities(kind="world_item"):
            attributes = dict(entity.attributes)
            kind = str(attributes.get("world_kind") or "")
            if kind not in COCKPIT_KINDS or (wanted is not None and kind not in wanted):
                continue
            fresh_until = float(attributes.get("fresh_until") or 0.0)
            freshness = "fresh" if fresh_until >= moment else "stale"
            if not include_stale and freshness == "stale":
                continue
            attributes.update(
                {
                    "item_id": entity.entity_id,
                    "freshness": freshness,
                    "observed_at": entity.observed_at,
                }
            )
            values.append(attributes)
        values.sort(
            key=lambda item: (
                -float(item.get("relevance") or 0.0),
                str(item.get("start_at") or ""),
                str(item.get("title") or ""),
            )
        )
        return values[: min(MAX_CARDS, max(1, int(limit)))]

    def provider_states(self, *, now: float | None = None) -> list[dict[str, Any]]:
        moment = _timestamp(now)
        values: list[dict[str, Any]] = []
        for entity in self.graph.entities(kind="service"):
            attributes = entity.attributes
            if not attributes.get("cockpit_provider"):
                continue
            state = str(attributes.get("state") or "unavailable")
            if state == "available" and float(attributes.get("fresh_until") or 0.0) < moment:
                state = "stale"
            values.append(
                {
                    "provider": attributes.get("provider"),
                    "kind": attributes.get("kind"),
                    "state": state,
                    "source_url": attributes.get("source_url") or "",
                    "fetched_at": attributes.get("fetched_at") or None,
                    "fresh_until": attributes.get("fresh_until") or None,
                    "reason": attributes.get("reason") or "",
                }
            )
        values.sort(key=lambda item: (str(item["kind"]), str(item["provider"])))
        return values

    def snapshot(self, *, now: float | None = None, limit: int = 40) -> dict[str, Any]:
        moment = _timestamp(now)
        items = self.items(now=moment, limit=limit)
        cards = [_evidence_card(item) for item in items]
        features = [_feature(card) for card in cards]
        features = [feature for feature in features if feature is not None]
        providers = self.provider_states(now=moment)
        return {
            "schema_version": 1,
            "generated_at": _iso(moment),
            "providers": providers,
            "cards": cards,
            "map_style": {
                "version": 8,
                "sources": {
                    "cockpit": {
                        "type": "geojson",
                        "data": {"type": "FeatureCollection", "features": features},
                    }
                },
                "layers": [
                    {
                        "id": "cockpit-points",
                        "type": "circle",
                        "source": "cockpit",
                        "paint": {
                            "circle-radius": 6,
                            "circle-color": "#78dce8",
                            "circle-stroke-color": "#10141c",
                            "circle-stroke-width": 1,
                        },
                    }
                ],
            },
            "voice_handoff": _voice_handoff(cards, providers),
        }

    def _store_provider(
        self,
        provider: str,
        *,
        kind: str,
        state: str,
        source_url: str,
        fetched_at: float,
        freshness_seconds: float,
        cached_records: Sequence[Mapping[str, Any]],
        reason: str,
        observed_at: float | None = None,
    ) -> GraphEntity:
        if state not in PROVIDER_STATES:
            raise WorldCockpitError("invalid provider state")
        observed = _timestamp(observed_at if observed_at is not None else fetched_at or None)
        freshness = _positive(freshness_seconds, "freshness_seconds")
        event_token = hashlib.sha256(
            json.dumps(
                {
                    "provider": provider,
                    "state": state,
                    "observed_at": observed,
                    "reason": reason,
                    "records": list(cached_records)[:MAX_PROVIDER_RECORDS],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:24]
        return self.graph.upsert_entity(
            _provider_id(provider),
            kind="service",
            name=f"Cockpit provider: {provider}",
            attributes={
                "cockpit_provider": True,
                "provider": provider,
                "kind": kind,
                "state": state,
                "source_url": source_url[:2_000],
                "fetched_at": fetched_at,
                "freshness_seconds": freshness,
                "fresh_until": fetched_at + freshness if fetched_at else 0.0,
                "cached_records": list(cached_records)[:MAX_PROVIDER_RECORDS],
                "reason": reason[:500],
                "online": state == "available",
            },
            source=f"cockpit:{provider}",
            observed_at=observed,
            ttl_seconds=freshness,
            event_id=f"cockpit-provider:{provider}:{event_token}",
        )

    def _store_item(
        self,
        record: dict[str, Any],
        *,
        provider: str,
        source_url: str,
    ) -> GraphEntity:
        entity_id = _item_id(str(record["dedupe_key"]))
        existing = self.graph.entity(entity_id)
        evidence = list(existing.attributes.get("evidence") or []) if existing else []
        new_evidence = {
            "provider": provider,
            "source_url": source_url[:2_000],
            "source_id": record.get("source_id") or "",
            "observed_at": record["observed_at"],
            "confidence": record["confidence"],
        }
        marker = (new_evidence["provider"], new_evidence["source_id"], new_evidence["observed_at"])
        evidence = [
            item
            for item in evidence
            if (item.get("provider"), item.get("source_id"), item.get("observed_at")) != marker
        ]
        evidence.append(new_evidence)
        evidence = evidence[-10:]
        merged = dict(existing.attributes) if existing else {}
        merged.update(record)
        merged["evidence"] = evidence
        merged["confidence"] = max(
            float(record["confidence"]),
            float(existing.attributes.get("confidence") or 0.0) if existing else 0.0,
        )
        event_token = hashlib.sha256(
            json.dumps(
                {"evidence": new_evidence, "record": record},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:24]
        item = self.graph.upsert_entity(
            entity_id,
            kind="world_item",
            name=str(record["title"]),
            attributes=merged,
            source=f"cockpit:{provider}",
            observed_at=float(record["observed_at"]),
            ttl_seconds=max(1.0, float(record["fresh_until"]) - float(record["observed_at"])),
            event_id=f"cockpit-item:{event_token}",
        )
        self.graph.upsert_edge(
            _provider_id(provider),
            "reports",
            entity_id,
            attributes={"confidence": record["confidence"]},
            source=f"cockpit:{provider}",
            observed_at=float(record["observed_at"]),
            ttl_seconds=max(1.0, float(record["fresh_until"]) - float(record["observed_at"])),
            event_id=f"cockpit-edge:{provider}:{event_token}",
        )
        return item

    def _normalize_record(
        self,
        raw: Mapping[str, Any],
        *,
        provider: str,
        kind: str,
        source_url: str,
        observed_at: float,
        ranked_at: float,
        freshness_seconds: float,
        default_confidence: float,
    ) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise WorldCockpitError("provider records must be objects")
        clean_kind = _kind(raw.get("kind") or kind)
        title = _text(raw.get("title"), "title", 500)
        summary = " ".join(str(raw.get("summary") or "").split())[:4_000]
        timezone_name = str(raw.get("timezone") or "UTC")
        start_at = _normalize_time(raw.get("start_at"), timezone_name)
        end_at = _normalize_time(raw.get("end_at"), timezone_name)
        if start_at and end_at and end_at < start_at:
            raise WorldCockpitError("end_at cannot be before start_at")
        location = _location(raw.get("location"), timezone_name)
        confidence = _confidence(raw.get("confidence", default_confidence))
        observed = _timestamp(float(raw.get("observed_at") or observed_at))
        ttl = _positive(raw.get("freshness_seconds", freshness_seconds), "freshness_seconds")
        source_id = " ".join(str(raw.get("source_id") or raw.get("external_id") or "").split())[
            :500
        ]
        url = _public_source_url(raw.get("url"))
        dedupe_key = _dedupe_key(
            clean_kind,
            title=title,
            start_at=start_at,
            location=location,
            url=url,
            explicit=raw.get("dedupe_key"),
        )
        relevance = _relevance(
            clean_kind,
            confidence=confidence,
            observed_at=observed,
            now=ranked_at,
            starts_at=start_at,
            explicit=raw.get("relevance"),
        )
        return {
            "world_kind": clean_kind,
            "title": title,
            "summary": summary,
            "start_at": start_at,
            "end_at": end_at,
            "location": location,
            "url": url,
            "source_id": source_id,
            "source_url": source_url[:2_000],
            "provider": provider,
            "observed_at": observed,
            "fresh_until": observed + ttl,
            "confidence": confidence,
            "relevance": relevance,
            "dedupe_key": dedupe_key,
            "online": raw.get("online") if clean_kind == "household" else None,
            "details": _bounded_details(raw.get("details")),
        }


def _validated_result(
    value: ProviderResult,
    *,
    expected_provider: str,
    expected_kind: str,
) -> ProviderResult:
    if not isinstance(value, ProviderResult):
        raise WorldCockpitError("adapter must return ProviderResult")
    if _provider(value.provider) != expected_provider or _kind(value.kind) != expected_kind:
        raise WorldCockpitError("adapter result identity does not match the adapter")
    if len(value.records) > MAX_PROVIDER_RECORDS:
        raise WorldCockpitError(f"provider returned more than {MAX_PROVIDER_RECORDS} records")
    _positive(value.freshness_seconds, "freshness_seconds")
    _confidence(value.confidence)
    return value


def _evidence_card(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": item["item_id"],
        "kind": item["world_kind"],
        "title": item["title"],
        "summary": item.get("summary") or "",
        "start_at": item.get("start_at"),
        "end_at": item.get("end_at"),
        "location": item.get("location"),
        "url": item.get("url") or "",
        "freshness": item["freshness"],
        "confidence": item["confidence"],
        "relevance": item["relevance"],
        "evidence": item.get("evidence") or [],
        "details": item.get("details") or {},
    }


def _feature(card: Mapping[str, Any]) -> dict[str, Any] | None:
    location = card.get("location")
    if not isinstance(location, Mapping):
        return None
    latitude, longitude = location.get("latitude"), location.get("longitude")
    if latitude is None or longitude is None:
        return None
    return {
        "type": "Feature",
        "id": card["id"],
        "geometry": {"type": "Point", "coordinates": [longitude, latitude]},
        "properties": {
            "kind": card["kind"],
            "title": card["title"],
            "freshness": card["freshness"],
            "confidence": card["confidence"],
        },
    }


def _voice_handoff(
    cards: Sequence[Mapping[str, Any]], providers: Sequence[Mapping[str, Any]]
) -> str:
    parts: list[str] = []
    for card in cards[:4]:
        summary = str(card.get("summary") or "").strip()
        phrase = str(card.get("title") or "")
        if summary:
            phrase += f": {summary}"
        phrase += f" ({card.get('freshness')}, confidence {float(card.get('confidence') or 0):.2f})"
        parts.append(phrase)
    unavailable = [
        str(item.get("provider")) for item in providers if item.get("state") == "unavailable"
    ]
    stale = [str(item.get("provider")) for item in providers if item.get("state") == "stale"]
    if unavailable:
        parts.append("Unavailable sources: " + ", ".join(unavailable))
    if stale:
        parts.append("Cached stale sources: " + ", ".join(stale))
    if not parts:
        return "The local world cockpit has no available evidence."
    return " ".join(parts)[:1_500]


def _provider(value: object) -> str:
    clean = str(value or "").strip().lower()
    if not _PROVIDER.fullmatch(clean):
        raise WorldCockpitError("provider must be a short lowercase identifier")
    return clean


def _kind(value: object) -> str:
    clean = str(value or "").strip().lower()
    if clean not in COCKPIT_KINDS:
        raise WorldCockpitError(f"unknown cockpit kind {clean!r}")
    return clean


def _text(value: object, label: str, limit: int) -> str:
    clean = " ".join(str(value or "").split())[:limit]
    if not clean:
        raise WorldCockpitError(f"{label} is required")
    return clean


def _timestamp(value: float | None) -> float:
    moment = float(time.time() if value is None else value)
    if not math.isfinite(moment) or moment < 0:
        raise WorldCockpitError("timestamp must be finite and non-negative")
    return moment


def _positive(value: object, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise WorldCockpitError(f"{label} must be positive")
    return number


def _timeout(value: object) -> float:
    timeout = _positive(value, "timeout_seconds")
    return min(timeout, 30.0)


def _confidence(value: object) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise WorldCockpitError("confidence must be between zero and one")
    return number


def _normalize_time(value: object, timezone_name: str) -> str | None:
    if value in {None, ""}:
        return None
    if isinstance(value, bool):
        raise WorldCockpitError("time values cannot be booleans")
    if isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        raw = str(value).strip()
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise WorldCockpitError(f"invalid ISO time {raw!r}") from exc
        if moment.tzinfo is None:
            try:
                moment = moment.replace(tzinfo=ZoneInfo(timezone_name))
            except ZoneInfoNotFoundError as exc:
                raise WorldCockpitError(f"unknown timezone {timezone_name!r}") from exc
    return _iso(moment.timestamp())


def _iso(moment: float) -> str:
    return (
        datetime.fromtimestamp(moment, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _location(value: object, timezone_name: str) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return {"label": " ".join(value.split())[:500], "timezone": timezone_name}
    if not isinstance(value, Mapping):
        raise WorldCockpitError("location must be text or an object")
    label = " ".join(str(value.get("label") or value.get("name") or "").split())[:500]
    latitude = value.get("latitude", value.get("lat"))
    longitude = value.get("longitude", value.get("lon"))
    if latitude is not None:
        latitude = float(latitude)
        if not math.isfinite(latitude) or not -90 <= latitude <= 90:
            raise WorldCockpitError("latitude is outside its valid range")
    if longitude is not None:
        longitude = float(longitude)
        if not math.isfinite(longitude) or not -180 <= longitude <= 180:
            raise WorldCockpitError("longitude is outside its valid range")
    if not label and latitude is None and longitude is None:
        raise WorldCockpitError("location must have a label or coordinates")
    return {
        "label": label,
        "latitude": latitude,
        "longitude": longitude,
        "timezone": str(value.get("timezone") or timezone_name)[:100],
    }


def _dedupe_key(
    kind: str,
    *,
    title: str,
    start_at: str | None,
    location: Mapping[str, Any] | None,
    url: str,
    explicit: object,
) -> str:
    if str(explicit or "").strip():
        raw = " ".join(str(explicit).casefold().split())[:1_000]
    else:
        location_key = ""
        if location:
            location_key = "|".join(
                str(location.get(key) or "").casefold()
                for key in ("label", "latitude", "longitude")
            )
        raw = "|".join(
            (kind, " ".join(title.casefold().split()), start_at or "", location_key, url)
        )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _item_id(dedupe_key: str) -> str:
    return f"world:{dedupe_key[:32]}"


def _provider_id(provider: str) -> str:
    return f"service:cockpit:{provider}"


def _relevance(
    kind: str,
    *,
    confidence: float,
    observed_at: float,
    now: float,
    starts_at: str | None,
    explicit: object,
) -> float:
    if explicit is not None:
        value = float(explicit)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise WorldCockpitError("relevance must be between zero and one")
        return round(value, 4)
    base = {"event": 0.25, "weather": 0.2, "household": 0.2, "news": 0.1}[kind]
    recency = 0.15 if observed_at >= now - 86_400 else 0.05
    upcoming = 0.15 if starts_at else 0.0
    return round(min(1.0, base + confidence * 0.5 + recency + upcoming), 4)


def _bounded_details(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise WorldCockpitError("details must be an object")
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True)
        clean = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise WorldCockpitError("details must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > 8_000:
        raise WorldCockpitError("details exceed 8000 bytes")
    return clean


def _cached_records(entity: GraphEntity | None) -> list[dict[str, Any]]:
    if entity is None:
        return []
    records = entity.attributes.get("cached_records")
    if not isinstance(records, list):
        return []
    return [dict(record) for record in records if isinstance(record, dict)][:MAX_PROVIDER_RECORDS]


def _bounded_error(error: BaseException) -> str:
    message = " ".join(redact_credentials(error).split())[:400]
    return f"{type(error).__name__}: {message or 'provider unavailable'}"


def _public_source_url(value: object) -> str:
    raw = str(value or "")[:2_000]
    parsed = urlsplit(raw)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        host = parsed.hostname
        if parsed.port is not None:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    return redact_credentials(raw)[:2_000]
