"""The local model Serena falls back to when both subscriptions are gone.

This is not a second brain and it is not pretending to be frontier reasoning. It
is the difference between "Serena is down" and "Serena is smaller today". The
role boundary is written into the profiles: a small model answers reflexes, a
mid model carries conversation, and the largest one that fits the card handles
the occasional thing that actually needs thinking.

Sizing is against the real machine, an RX 6800 XT with 16 GB of VRAM and 32 GB
of system RAM, with headroom left for the desktop compositor. A profile that
does not fit is reported as not fitting rather than loaded and left to thrash
into system memory, because a model that takes ninety seconds to answer is worse
than an honest "I cannot do that right now".

Two hard rules are enforced in code rather than trusted to callers:

The endpoint must be loopback. Private conversation is the entire reason this
exists, and a "local" model reachable at someone else's IP address is just a
hosted provider with a friendlier name, so a non-loopback URL fails closed.

A local answer is always labelled local. Every result carries the provider and
the actual model id that produced it. There is no code path that lets a local
reply inherit a cloud model's name, and `assert_local_provenance` exists so a
caller can prove that before showing anything to Raghav.

Nothing here downloads weights. `weight_instruction` returns the command a human
runs; this module will not fetch several gigabytes on its own initiative.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

# The real card and the real box. Everything below is sized against these.
DEFAULT_VRAM_GB = 16.0
DEFAULT_RAM_GB = 32.0
# The desktop, the browser, and the compositor are also using this GPU. Handing
# the model the whole card is how you get a frozen session instead of a fallback.
DEFAULT_RESERVED_VRAM_GB = 1.5

DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_PROBE_TIMEOUT_SECONDS = 3.0
# Unload after idle so gaming or a heavy build gets the VRAM back without
# anybody having to remember to stop a service.
DEFAULT_KEEP_ALIVE = "5m"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0:0:0:0:0:0:0:1"})

ROLES = ("reflex", "conversation", "reasoning")


class LocalModelUnavailable(RuntimeError):
    """The local model cannot serve this request, and the reason is stated."""


class ProvenanceError(AssertionError):
    """Something tried to describe a result as coming from a model it did not."""


@dataclass(frozen=True, slots=True)
class HardwareBudget:
    vram_gb: float = DEFAULT_VRAM_GB
    ram_gb: float = DEFAULT_RAM_GB
    reserved_vram_gb: float = DEFAULT_RESERVED_VRAM_GB

    @property
    def usable_vram_gb(self) -> float:
        return max(0.0, self.vram_gb - self.reserved_vram_gb)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["usable_vram_gb"] = self.usable_vram_gb
        return value


@dataclass(frozen=True, slots=True)
class LocalModelProfile:
    """One open-weight model, its role, and what it costs to hold on the card."""

    role: str
    model_id: str
    parameters_b: float
    quantization: str
    approx_vram_gb: float
    context_tokens: int
    notes: str = ""

    def fits(self, budget: HardwareBudget) -> bool:
        return self.approx_vram_gb <= budget.usable_vram_gb

    def headroom_gb(self, budget: HardwareBudget) -> float:
        return round(budget.usable_vram_gb - self.approx_vram_gb, 2)

    def to_dict(self, budget: HardwareBudget | None = None) -> dict[str, Any]:
        value = asdict(self)
        if budget is not None:
            value["fits"] = self.fits(budget)
            value["headroom_gb"] = self.headroom_gb(budget)
        return value


# Ordered smallest first inside each role. Every VRAM figure is the weights plus
# a working KV cache at the stated context, rounded up, not the bare file size.
PROFILES: tuple[LocalModelProfile, ...] = (
    LocalModelProfile(
        role="reflex",
        model_id="qwen2.5:3b-instruct-q4_K_M",
        parameters_b=3.0,
        quantization="Q4_K_M",
        approx_vram_gb=2.6,
        context_tokens=8_192,
        notes="wake acknowledgements, intent classification, short local commands",
    ),
    LocalModelProfile(
        role="conversation",
        model_id="qwen2.5:14b-instruct-q4_K_M",
        parameters_b=14.0,
        quantization="Q4_K_M",
        approx_vram_gb=9.6,
        context_tokens=16_384,
        notes="ordinary spoken conversation and memory recall while cloud is out",
    ),
    LocalModelProfile(
        role="reasoning",
        model_id="gpt-oss:20b",
        parameters_b=20.0,
        quantization="MXFP4",
        approx_vram_gb=13.0,
        context_tokens=8_192,
        notes="tool-capable degraded reasoning; the largest that still fits 16 GB",
    ),
)

_BY_ROLE = {profile.role: profile for profile in PROFILES}


def select_profile(
    role: str = "conversation",
    *,
    budget: HardwareBudget | None = None,
) -> LocalModelProfile:
    """The profile for a role, or a refusal that says why it does not fit."""

    resolved = budget or HardwareBudget()
    clean = str(role or "").strip().lower()
    profile = _BY_ROLE.get(clean)
    if profile is None:
        raise LocalModelUnavailable(
            f"unknown local model role {clean!r}; expected one of {', '.join(ROLES)}"
        )
    if not profile.fits(resolved):
        raise LocalModelUnavailable(
            f"{profile.model_id} needs about {profile.approx_vram_gb} GB of VRAM and "
            f"only {resolved.usable_vram_gb} GB is usable on this card"
        )
    return profile


def fitting_profiles(budget: HardwareBudget | None = None) -> list[LocalModelProfile]:
    resolved = budget or HardwareBudget()
    return [profile for profile in PROFILES if profile.fits(resolved)]


def weight_instruction(profile: LocalModelProfile) -> str:
    """How a human puts these weights on the machine. This never runs it."""

    return f"ollama pull {profile.model_id}"


@dataclass(frozen=True, slots=True)
class LocalModelStatus:
    available: bool
    reason: str
    base_url: str = ""
    served_models: tuple[str, ...] = ()
    checked_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["served_models"] = list(self.served_models)
        return value


_SIZE_TOKEN = re.compile(r"(?:^|[:\-_])(\d+(?:\.\d+)?)b(?=$|[\-_.])", re.IGNORECASE)


def _family_and_size(model_id: str) -> tuple[str, float | None]:
    """Split a model id into its family stem and declared parameter count.

    `qwen2.5:14b-instruct-q4_K_M` is ("qwen2.5", 14.0). `qwen2.5:latest` is
    ("qwen2.5", None), meaning the served name refuses to say how big it is,
    which is treated as unverifiable rather than as a match.
    """

    stem, _, tag = str(model_id or "").strip().partition(":")
    found = _SIZE_TOKEN.search(tag)
    if found is None:
        return stem.lower(), None
    try:
        return stem.lower(), float(found.group(1))
    except ValueError:  # pragma: no cover - the regex only captures numbers
        return stem.lower(), None


def _wrong_size_note(profile: LocalModelProfile, served: tuple[str, ...]) -> str:
    """Name a same-family model of the wrong size, because that is the trap."""

    stem, wanted = _family_and_size(profile.model_id)
    if wanted is None:
        return ""
    for candidate in served:
        family, size = _family_and_size(candidate)
        if family == stem and size is not None and abs(size - wanted) >= 1e-6:
            return (
                f"; {candidate} is served but it is a {size:g}b model and this "
                f"profile needs {wanted:g}b"
            )
    return ""


def _require_loopback(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise LocalModelUnavailable(
            f"local model url must be http or https, got {parsed.scheme or 'nothing'}"
        )
    host = (parsed.hostname or "").strip().lower()
    if host not in _LOOPBACK_HOSTS:
        raise LocalModelUnavailable(
            f"local model url must stay on this machine; {host or 'that host'} is not "
            "loopback, and sending private conversation there would make it a hosted "
            "provider wearing a local label"
        )
    return base_url.rstrip("/")


Opener = Callable[[urllib.request.Request, float], bytes]


def _urlopen(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


class LocalModelEndpoint:
    """A local OpenAI-compatible server, addressed carefully and bounded."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        profile: LocalModelProfile | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
        keep_alive: str = DEFAULT_KEEP_ALIVE,
        opener: Opener | None = None,
    ) -> None:
        configured = base_url or os.environ.get("SERENA_LOCAL_MODEL_URL", "").strip()
        self.base_url = _require_loopback(configured or DEFAULT_BASE_URL)
        self.profile = profile or _BY_ROLE["conversation"]
        self.timeout = float(timeout)
        self.probe_timeout = float(probe_timeout)
        self.keep_alive = keep_alive
        self._opener: Opener = opener or _urlopen

    # -- availability -------------------------------------------------------

    def probe(self, *, now: float | None = None) -> LocalModelStatus:
        """Is anything actually serving, and does it have our weights?

        Never raises. A fallback path that can throw while deciding whether the
        fallback exists is not a fallback.
        """

        moment = float(time.time() if now is None else now)
        request = urllib.request.Request(  # noqa: S310 - loopback enforced above
            f"{self.base_url}/models", method="GET"
        )
        try:
            raw = self._opener(request, self.probe_timeout)
        except (urllib.error.URLError, OSError, ValueError) as error:
            return LocalModelStatus(
                False,
                f"no local model server answering at {self.base_url} ({_short(error)})",
                base_url=self.base_url,
                checked_at=moment,
            )
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, AttributeError):
            return LocalModelStatus(
                False,
                f"local model server at {self.base_url} returned something unreadable",
                base_url=self.base_url,
                checked_at=moment,
            )
        served = tuple(
            str(item.get("id") or "")
            for item in (payload.get("data") or [])
            if isinstance(item, dict) and item.get("id")
        )
        if not served:
            return LocalModelStatus(
                False,
                f"local model server is up but serving nothing; run "
                f"{weight_instruction(self.profile)}",
                base_url=self.base_url,
                checked_at=moment,
            )
        if not self._match(served):
            return LocalModelStatus(
                False,
                f"{self.profile.model_id} is not loaded{_wrong_size_note(self.profile, served)}"
                f"; run {weight_instruction(self.profile)}",
                base_url=self.base_url,
                served_models=served,
                checked_at=moment,
            )
        return LocalModelStatus(
            True,
            f"{self.profile.model_id} is served locally at {self.base_url}",
            base_url=self.base_url,
            served_models=served,
            checked_at=moment,
        )

    def _match(self, served: tuple[str, ...]) -> str:
        """Accept a served name only when it is provably the same size model.

        The family stem alone is not identity. `qwen2.5:3b-instruct-q4_K_M` and
        `qwen2.5:14b-instruct-q4_K_M` share the stem `qwen2.5`, so matching on
        the stem let a 3b answer every request the 14b conversation profile
        promised. That is the exact failure this whole module exists to prevent:
        it is not a crash, it is Serena quietly getting worse at speaking while
        still reporting the model she meant to be running.

        So an alias is accepted only when the served tag states a parameter
        count and that count is the profile's. A tag that states nothing, like
        `qwen2.5:latest`, is unverifiable and therefore refused; the honest
        answer is to name the exact model and let a human pull it.
        """

        target = self.profile.model_id
        if target in served:
            return target
        stem, wanted = _family_and_size(target)
        if wanted is None:
            return ""
        for candidate in served:
            family, size = _family_and_size(candidate)
            if family == stem and size is not None and abs(size - wanted) < 1e-6:
                return candidate
        return ""

    # -- inference ----------------------------------------------------------

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
    ) -> dict[str, Any]:
        """One bounded local completion, labelled with what actually produced it."""

        body = json.dumps(
            {
                "model": self.profile.model_id,
                "messages": messages,
                "max_tokens": int(max_tokens),
                "temperature": float(temperature),
                "stream": False,
                "keep_alive": self.keep_alive,
            }
        ).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - loopback enforced above
            f"{self.base_url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            raw = self._opener(request, self.timeout)
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise LocalModelUnavailable(
                f"local model call failed: {_short(error)}"
            ) from error
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, AttributeError) as error:
            raise LocalModelUnavailable(
                "local model returned a response that was not json"
            ) from error
        choices = payload.get("choices") or []
        if not choices:
            raise LocalModelUnavailable("local model returned no completion")
        message = choices[0].get("message") or {}
        text = str(message.get("content") or "").strip()
        if not text:
            raise LocalModelUnavailable("local model returned an empty completion")
        # The served name wins over the requested name. If the server answered
        # with a different model than we asked for, that is what Raghav sees.
        actual = str(payload.get("model") or self.profile.model_id)
        return {
            "text": text,
            "provider": "local",
            "model": actual,
            "role": self.profile.role,
            "origin": "local-model",
            "base_url": self.base_url,
        }


class LocalBrain:
    """The codex-brain shape, backed by the local endpoint.

    Deliberately mirrors `start`/`turn`/`interrupt`/`close`/`snapshot` so the
    resident brain can hold this in the same slot it already holds a Codex
    worker in. Matching an existing contract is what keeps this a fallback lane
    instead of a second architecture.
    """

    def __init__(
        self,
        endpoint: LocalModelEndpoint | None = None,
        *,
        system_prompt: str = "",
    ) -> None:
        self.endpoint = endpoint or LocalModelEndpoint()
        self.system_prompt = system_prompt
        self.started = False
        self._status: LocalModelStatus | None = None

    @property
    def model(self) -> str:
        return self.endpoint.profile.model_id

    async def start(self) -> None:
        status = self.endpoint.probe()
        self._status = status
        if not status.available:
            raise LocalModelUnavailable(status.reason)
        self.started = True

    async def turn(self, message: str, *, on_delta: Any | None = None) -> dict[str, Any]:
        if not self.started:
            await self.start()
        messages: list[dict[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": str(message)})
        result = self.endpoint.complete(messages)
        if on_delta is not None:
            outcome = on_delta(result["text"])
            if hasattr(outcome, "__await__"):
                await outcome
        return result

    async def interrupt(self) -> None:
        return None

    async def close(self) -> None:
        self.started = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "running": self.started,
            "provider": "local",
            "model": self.endpoint.profile.model_id,
            "role": self.endpoint.profile.role,
            "base_url": self.endpoint.base_url,
            "status": self._status.to_dict() if self._status else None,
        }


def assert_local_provenance(result: dict[str, Any]) -> dict[str, Any]:
    """Refuse to pass along a local result wearing a cloud label.

    Cheap to call and worth calling everywhere a result crosses into a surface.
    The failure this prevents is the one that destroys trust fastest: Raghav
    being told a small local model's answer came from Opus, or the reverse.
    """

    provider = str(result.get("provider") or "")
    if provider != "local":
        raise ProvenanceError(
            f"a local model result must be labelled provider 'local', got {provider!r}"
        )
    model = str(result.get("model") or "")
    if not model:
        raise ProvenanceError("a local model result must name the model that produced it")
    if model in _CLOUD_MODEL_NAMES:
        raise ProvenanceError(
            f"{model} is a cloud model name; a local result must never claim it"
        )
    return result


# Names that can only ever come from a subscription provider. Used purely to
# catch a mislabelled result, never to select anything.
_CLOUD_MODEL_NAMES = frozenset(
    {
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-fable-5",
        "claude-haiku-4-5-20251001",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    }
)


def local_status(
    *,
    role: str = "conversation",
    budget: HardwareBudget | None = None,
    endpoint: LocalModelEndpoint | None = None,
    now: float | None = None,
) -> tuple[LocalModelProfile | None, LocalModelStatus]:
    """One call for "is there a usable local brain", safe to run anywhere."""

    resolved = budget or HardwareBudget()
    moment = float(time.time() if now is None else now)
    try:
        profile = select_profile(role, budget=resolved)
    except LocalModelUnavailable as error:
        return None, LocalModelStatus(False, str(error), checked_at=moment)
    try:
        target = endpoint or LocalModelEndpoint(profile=profile)
    except LocalModelUnavailable as error:
        return profile, LocalModelStatus(False, str(error), checked_at=moment)
    return profile, target.probe(now=moment)


def _short(value: object, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def hardware_report(budget: HardwareBudget | None = None) -> dict[str, Any]:
    """What this machine can hold, for a status surface or a doc."""

    resolved = budget or HardwareBudget()
    with suppress(Exception):
        return {
            "budget": resolved.to_dict(),
            "profiles": [profile.to_dict(resolved) for profile in PROFILES],
            "fitting": [profile.model_id for profile in fitting_profiles(resolved)],
            "downloads_performed": False,
            "weight_instructions": [
                weight_instruction(profile) for profile in fitting_profiles(resolved)
            ],
        }
    return {}  # pragma: no cover - defensive


__all__ = [
    "DEFAULT_BASE_URL",
    "PROFILES",
    "ROLES",
    "HardwareBudget",
    "LocalBrain",
    "LocalModelEndpoint",
    "LocalModelProfile",
    "LocalModelStatus",
    "LocalModelUnavailable",
    "ProvenanceError",
    "assert_local_provenance",
    "fitting_profiles",
    "hardware_report",
    "local_status",
    "select_profile",
    "weight_instruction",
]
