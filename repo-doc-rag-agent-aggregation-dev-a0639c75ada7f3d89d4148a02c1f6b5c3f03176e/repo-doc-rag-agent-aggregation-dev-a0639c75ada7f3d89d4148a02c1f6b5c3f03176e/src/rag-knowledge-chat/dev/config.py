from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")


@dataclass(frozen=True, slots=True)
class DevPanelConfig:
    chat_base_url: str
    api_key: str


def load_dev_panel_config(path: Path = DEFAULT_CONFIG_PATH) -> DevPanelConfig:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"dev panel config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid dev panel YAML: {path}") from exc

    if not isinstance(payload, dict):
        raise ValueError("chat_base_url must be configured in dev/config.yaml")
    value = payload.get("chat_base_url")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("chat_base_url must be configured in dev/config.yaml")

    base_url = value.strip().rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("chat_base_url must be an absolute HTTP(S) URL")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("chat_base_url must not contain credentials, query, or fragment")

    api_key = payload.get("api_key")
    if not isinstance(api_key, str):
        raise ValueError("api_key must be a string in dev/config.yaml")
    return DevPanelConfig(chat_base_url=base_url, api_key=api_key.strip())
