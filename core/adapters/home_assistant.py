"""The house, over Home Assistant or plain MQTT, when either one exists.

Neither is set up on this machine right now. That is the normal case for this
adapter and it is handled the same way as a phone that is not plugged in: the
status says so plainly and nothing pretends to have happened. The point of
writing it now is that the contract, the safety rules, and the tests are ready
before the hardware arrives, so plugging in a broker later is configuration
rather than a new subsystem.

Matter devices are reached the same way. Home Assistant's Matter integration
exposes them as ordinary entities, so `light.kitchen` is the same call whether
the bulb speaks Zigbee, Matter, or Wi-Fi. There is no separate Matter path
here on purpose.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from core.adapters.base import AdapterStatus, DeviceCommand, DeviceResult

_CAPABILITIES = {
    "home.states": "read",
    "home.entity_state": "read",
    "home.light_on": "external",
    "home.light_off": "external",
    "home.switch_on": "external",
    "home.switch_off": "external",
    "home.scene_activate": "external",
    "home.media_pause": "external",
}

_SERVICES = {
    "home.light_on": ("light", "turn_on"),
    "home.light_off": ("light", "turn_off"),
    "home.switch_on": ("switch", "turn_on"),
    "home.switch_off": ("switch", "turn_off"),
    "home.scene_activate": ("scene", "turn_on"),
    "home.media_pause": ("media_player", "media_pause"),
}

_COMPENSATIONS = {
    "home.light_on": "home.light_off",
    "home.light_off": "home.light_on",
    "home.switch_on": "home.switch_off",
    "home.switch_off": "home.switch_on",
}

_ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


class HomeAssistantAdapter:
    """REST against a local Home Assistant. Local network only, by policy."""

    name = "home"

    def __init__(
        self,
        *,
        base_url: str = "",
        token: str = "",
        opener=None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = (base_url or os.environ.get("SERENA_HOME_ASSISTANT_URL", "")).strip().rstrip("/")
        self._token = token or os.environ.get("SERENA_HOME_ASSISTANT_TOKEN", "")
        self._opener = opener
        self._timeout = float(timeout_seconds)

    def capabilities(self) -> Mapping[str, str]:
        return dict(_CAPABILITIES)

    def configured(self) -> bool:
        return bool(self._base_url and self._token)

    def status(self) -> AdapterStatus:
        if not self._base_url:
            return AdapterStatus(
                False, "no Home Assistant URL is configured (SERENA_HOME_ASSISTANT_URL)"
            )
        if not self._token:
            return AdapterStatus(
                False, "no Home Assistant token is configured (SERENA_HOME_ASSISTANT_TOKEN)"
            )
        try:
            payload = self._call("GET", "/api/", None, self._timeout)
        except _HomeAssistantError as error:
            return AdapterStatus(False, str(error))
        return AdapterStatus(True, "", {"api": payload if isinstance(payload, dict) else {}})

    def describe(self, command: DeviceCommand) -> str:
        if command.capability in _SERVICES:
            domain, service = _SERVICES[command.capability]
            return f"call {domain}.{service} on {command.target or 'the house'}"
        return f"read {command.target or 'household state'} from Home Assistant"

    def execute(self, command: DeviceCommand) -> DeviceResult:
        if command.capability not in _CAPABILITIES:
            return _failed(command, "unsupported", "not a Home Assistant capability")
        available = self.status()
        if not available.available:
            return DeviceResult(
                False, "unavailable", command.capability, command.target, available.reason
            )
        timeout = max(1.0, float(command.timeout_seconds))
        try:
            if command.capability == "home.states":
                body = self._call("GET", "/api/states", None, timeout)
                return DeviceResult(
                    True, "completed", command.capability, command.target,
                    f"{len(body) if isinstance(body, list) else 0} entities",
                    receipt={"entities": len(body) if isinstance(body, list) else 0},
                )
            entity = str(command.target or "").strip()
            if not _ENTITY.fullmatch(entity):
                return _failed(
                    command, "rejected", "a Home Assistant entity id looks like light.kitchen"
                )
            if command.capability == "home.entity_state":
                body = self._call("GET", f"/api/states/{entity}", None, timeout)
                state = body.get("state") if isinstance(body, Mapping) else None
                return DeviceResult(
                    True, "completed", command.capability, entity, str(state or ""),
                    receipt={"state": state},
                )
            domain, service = _SERVICES[command.capability]
            if entity.split(".", 1)[0] != domain:
                return _failed(
                    command, "rejected", f"{entity} is not a {domain} entity"
                )
            body = self._call(
                "POST", f"/api/services/{domain}/{service}", {"entity_id": entity}, timeout
            )
            return DeviceResult(
                True, "completed", command.capability, entity,
                f"called {domain}.{service}",
                receipt={"changed": body if isinstance(body, list) else []},
            )
        except _HomeAssistantError as error:
            return _failed(command, "failed", str(error))

    def compensate(self, command: DeviceCommand) -> DeviceResult | None:
        inverse = _COMPENSATIONS.get(command.capability)
        if inverse is None:
            return None
        return self.execute(
            DeviceCommand(
                capability=inverse,
                target=command.target,
                params=dict(command.params),
                timeout_seconds=command.timeout_seconds,
            )
        )

    def postcondition(self, command: DeviceCommand) -> bool | None:
        expected = {
            "home.light_on": "on",
            "home.light_off": "off",
            "home.switch_on": "on",
            "home.switch_off": "off",
        }.get(command.capability)
        if expected is None:
            return None
        try:
            body = self._call("GET", f"/api/states/{command.target}", None, self._timeout)
        except _HomeAssistantError:
            return None
        if not isinstance(body, Mapping):
            return None
        return str(body.get("state") or "").lower() == expected

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None, timeout: float) -> Any:
        url = f"{self._base_url}{path}"
        self._validate(url)
        data = json.dumps(dict(body)).encode("utf-8") if body is not None else None
        request = urlrequest.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        opener = self._opener or _no_redirect_opener().open
        try:
            with opener(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urlerror.HTTPError as error:
            raise _HomeAssistantError(f"Home Assistant returned {error.code}") from error
        except (urlerror.URLError, OSError, TimeoutError) as error:
            raise _HomeAssistantError(
                f"{type(error).__name__}: Home Assistant is unreachable"
            ) from error
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as error:
            raise _HomeAssistantError("Home Assistant returned something that is not JSON") from error

    @staticmethod
    def _validate(url: str) -> None:
        """A house controller lives on the LAN, so private addresses are the point.

        core.security_policy's URL policy exists to stop a model reaching
        private infrastructure. Here that is exactly the intended target, so
        this opts in explicitly and narrowly instead of quietly skipping the
        check: http is allowed, the host must be the configured one, and a
        redirect is never followed.
        """

        from urllib.parse import urlsplit

        from core.security_policy import SecurityPolicyError, URLPolicy

        host = urlsplit(url).hostname or ""
        if not host:
            raise _HomeAssistantError("the configured Home Assistant URL has no host")
        policy = URLPolicy(
            allowed_domains=(host,), allow_private_network=True, allow_http=True
        )
        try:
            policy.validate(url)
        except SecurityPolicyError as error:
            raise _HomeAssistantError(str(error)) from error


class MqttAdapter:
    """Plain MQTT publish, for a house with a broker but no Home Assistant.

    paho-mqtt is a soft import. It is not a dependency of Serena and this run
    does not add one, so with no client library installed this adapter simply
    reports itself unavailable, which is the honest answer.
    """

    name = "mqtt"

    def __init__(
        self,
        *,
        host: str = "",
        port: int = 1883,
        client_factory=None,
        topic_prefix: str = "serena",
    ) -> None:
        self._host = (host or os.environ.get("SERENA_MQTT_HOST", "")).strip()
        self._port = int(port or os.environ.get("SERENA_MQTT_PORT", "1883") or 1883)
        self._client_factory = client_factory
        self._prefix = str(topic_prefix or "serena").strip("/")

    def capabilities(self) -> Mapping[str, str]:
        return {"mqtt.publish": "external"}

    def status(self) -> AdapterStatus:
        if not self._host:
            return AdapterStatus(False, "no MQTT broker is configured (SERENA_MQTT_HOST)")
        if self._client_factory is None and not _paho_available():
            return AdapterStatus(
                False, "paho-mqtt is not installed, so MQTT publishing is unavailable"
            )
        return AdapterStatus(True, "", {"host": self._host, "port": self._port})

    def describe(self, command: DeviceCommand) -> str:
        topic = self._topic(command.target)
        if topic is None:
            return f"refuse to publish: {command.target!r} is not a usable MQTT topic"
        return f"publish to {topic} on the MQTT broker"

    def execute(self, command: DeviceCommand) -> DeviceResult:
        if command.capability != "mqtt.publish":
            return _failed(command, "unsupported", "not an MQTT capability")
        available = self.status()
        if not available.available:
            return DeviceResult(
                False, "unavailable", command.capability, command.target, available.reason
            )
        topic = self._topic(command.target)
        if topic is None:
            # Rewriting a bad topic to a valid one and reporting success would
            # publish somewhere nobody asked for and call it done.
            return _failed(
                command,
                "rejected",
                "an MQTT topic cannot be empty or contain '..', '#', or '+'",
            )
        payload = str(command.params.get("payload") or "")
        try:
            client = self._client()
            client.connect(self._host, self._port, int(command.timeout_seconds) or 10)
            try:
                client.publish(topic, payload)
            finally:
                client.disconnect()
        except Exception as error:
            return _failed(command, "failed", f"{type(error).__name__}: MQTT publish failed")
        return DeviceResult(
            True, "completed", command.capability, topic, "published",
            receipt={"topic": topic, "bytes": len(payload)},
        )

    def compensate(self, command: DeviceCommand) -> DeviceResult | None:
        # A published message cannot be recalled. Saying so is the honest answer.
        return None

    def postcondition(self, command: DeviceCommand) -> bool | None:
        return None

    def _topic(self, target: str) -> str | None:
        """None means "not a topic", which is refused rather than redirected."""

        clean = str(target or "").strip().strip("/")
        if not clean or ".." in clean or "#" in clean or "+" in clean:
            return None
        return f"{self._prefix}/{clean}"

    def _client(self):
        if self._client_factory is not None:
            return self._client_factory()
        import paho.mqtt.client as mqtt  # noqa: PLC0415

        return mqtt.Client()


class _BlockRedirects(urlrequest.HTTPRedirectHandler):
    """A bearer token goes to the configured host or nowhere.

    `urlopen` follows redirects by default, which would let whatever answers on
    the configured address send the Authorization header somewhere else with a
    single 302. The URL policy validates the address that was asked for, so it
    cannot help once a response redirects past it. Refusing outright is the only
    version of "never follows a redirect" that is actually true.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        raise urlerror.HTTPError(
            req.full_url,
            code,
            f"Home Assistant tried to redirect to {newurl}, which is never followed",
            headers,
            fp,
        )


def _no_redirect_opener() -> urlrequest.OpenerDirector:
    return urlrequest.build_opener(_BlockRedirects())


class _HomeAssistantError(RuntimeError):
    pass


def _failed(command: DeviceCommand, status: str, detail: str) -> DeviceResult:
    return DeviceResult(False, status, command.capability, command.target, detail)


def _paho_available() -> bool:
    try:
        import paho.mqtt.client  # noqa: F401,PLC0415

        return True
    except ImportError:
        return False
