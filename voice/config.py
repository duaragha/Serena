from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class WakeWordConfig:
    model_path: str = "models/hey_serena.onnx"
    threshold: float = 0.7


@dataclass
class STTConfig:
    engine: str = "groq"  # "groq" (cloud, fast, accurate) or "local" (faster-whisper)
    model: str = "whisper-large-v3-turbo"  # for groq; or "base.en"/"small.en" for local
    device: str = "cpu"
    language: str = "en"


@dataclass
class TTSConfig:
    engine: str = "piper"
    piper_model: str = "en_US-amy-medium"
    speed: float = 1.1


@dataclass
class LLMConfig:
    default_model: str = "claude-sonnet-4-6"
    complex_model: str = "claude-opus-4-6"
    max_context_turns: int = 20


@dataclass
class DaemonConfig:
    morning_briefing: str = "07:30"
    evening_summary: str = "18:00"
    meeting_prep_minutes: int = 15
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "07:00"
    daily_message_cap: int = 8
    hourly_message_cap: int = 2
    cooldown_minutes: int = 10


@dataclass
class CalendarConfig:
    credentials_path: str = "~/.config/serena/google_credentials.json"
    poll_interval_minutes: int = 5


@dataclass
class EmailConfig:
    imap_server: str = ""
    username: str = ""
    app_password: str = ""


@dataclass
class WeatherConfig:
    latitude: float = 43.6834
    longitude: float = -79.7663
    units: str = "celsius"


@dataclass
class UIConfig:
    overlay_opacity: float = 0.85
    animation_fps: int = 30
    dashboard_enabled: bool = True


@dataclass
class NotificationsConfig:
    ntfy_topic: str = ""


@dataclass
class SerenaConfig:
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)


def _dict_to_dataclass(cls, data: dict):
    if not data:
        return cls()
    filtered = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
    return cls(**filtered)


def load_config(path: str | Path = "config.yaml") -> SerenaConfig:
    path = Path(path)
    if not path.exists():
        return SerenaConfig()

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    return SerenaConfig(
        wake_word=_dict_to_dataclass(WakeWordConfig, raw.get("wake_word")),
        stt=_dict_to_dataclass(STTConfig, raw.get("stt")),
        tts=_dict_to_dataclass(TTSConfig, raw.get("tts")),
        llm=_dict_to_dataclass(LLMConfig, raw.get("llm")),
        daemon=_dict_to_dataclass(DaemonConfig, raw.get("daemon")),
        calendar=_dict_to_dataclass(CalendarConfig, raw.get("calendar")),
        email=_dict_to_dataclass(EmailConfig, raw.get("email")),
        weather=_dict_to_dataclass(WeatherConfig, raw.get("weather")),
        ui=_dict_to_dataclass(UIConfig, raw.get("ui")),
        notifications=_dict_to_dataclass(NotificationsConfig, raw.get("notifications")),
    )
