"""Base desktop installs must include unconditional scheduler dependencies."""
from pathlib import Path
import tomllib

from packaging.requirements import Requirement


def test_knowledge_yaml_is_not_hidden_in_an_optional_voice_extra():
    config = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    base = {Requirement(value).name.lower() for value in config["project"]["dependencies"]}
    assert "pyyaml" in base

    from core.knowledge_store import yaml

    assert yaml.safe_load("enabled: true") == {"enabled": True}
