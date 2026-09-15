from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import AppSettings, DevPanelSettings
from app.llm.gateway import LLMGateway

ROOT = Path(__file__).parents[4]
PROJECT = ROOT / "src" / "wechat-article-agent"


def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        result[key] = value
    return result


def _aliases(model: type[AppSettings] | type[DevPanelSettings]) -> set[str]:
    return {str(field.alias) for field in model.model_fields.values()}


def test_environment_templates_match_settings_contract() -> None:
    production = _read_env(ROOT / "env" / "wechat-article.env.example")
    local = _read_env(PROJECT / ".env.example")
    app_aliases = _aliases(AppSettings)
    dev_aliases = _aliases(DevPanelSettings)

    assert set(production) == app_aliases
    assert set(local) == app_aliases | dev_aliases
    assert not any(key.startswith("DEV_") for key in production)


def test_templates_have_expected_environment_semantics() -> None:
    production = _read_env(ROOT / "env" / "wechat-article.env.example")
    local = _read_env(PROJECT / ".env.example")

    assert production["APP_PORT"] == "8140"
    assert int(production["APP_MAX_ACTIVE_RUNS"]) >= 30
    assert int(production["APP_MAX_QUEUED_RUNS"]) >= 90
    assert int(production["ARK_MAX_CONCURRENCY"]) >= 30
    assert int(production["DB_POOL_MAX"]) >= 30
    for key in ("DATABASE_URL", "AGENT_SERVER_URL", "RETRIEVAL_BASE_URL"):
        assert "localhost" not in production[key]
        assert "127.0.0.1" not in production[key]

    assert local["APP_PORT"] == "8240"
    assert local["AGENT_SERVER_URL"] == "http://127.0.0.1:8242"
    assert local["RETRIEVAL_BASE_URL"] == "http://127.0.0.1:8220"
    assert production["SEEDREAM_MODEL"] == "doubao-seedream-5-0-260128"
    assert local["SEEDREAM_MODEL"] == "doubao-seedream-5-0-260128"
    assert production["HTML_LAYOUT_RENDERER"] == "skill_driven"
    assert local["HTML_LAYOUT_RENDERER"] == "skill_driven"


def test_only_three_generation_phases_can_enable_thinking() -> None:
    assert LLMGateway._THINKING_PHASES == {"task_spec", "outline", "article"}
    assert LLMGateway._OUTPUT_REPAIR_PHASES == {"task_spec", "outline", "article"}


def test_templates_do_not_revive_removed_state_systems() -> None:
    values = {
        **_read_env(ROOT / "env" / "wechat-article.env.example"),
        **_read_env(PROJECT / ".env.example"),
    }
    forbidden = ("REDIS", "IDEMPOTENCY", "DELIVERY_BUFFER", "EVENT_REPLAY")
    assert not [key for key in values if any(name in key for name in forbidden)]


def test_templates_load_without_secret() -> None:
    production = _read_env(ROOT / "env" / "wechat-article.env.example")
    local = _read_env(PROJECT / ".env.example")
    production["ARK_API_KEY"] = ""
    local["ARK_API_KEY"] = ""
    assert AppSettings.model_validate(production).app_port == 8140
    assert AppSettings.model_validate(local).app_port == 8240
    assert DevPanelSettings.model_validate(local).dev_panel_port == 8245


def test_unregistered_default_layout_theme_is_rejected_during_settings_validation() -> None:
    local = _read_env(PROJECT / ".env.example")
    local["HTML_LAYOUT_DEFAULT_THEME"] = "missing-theme"

    with pytest.raises(ValidationError, match="HTML_LAYOUT_DEFAULT_THEME"):
        AppSettings.model_validate(local)


@pytest.mark.parametrize("renderer", ["deterministic", "llm_decide", "skill_driven"])
def test_supported_layout_renderer_modes_are_accepted(renderer: str) -> None:
    local = _read_env(PROJECT / ".env.example")
    local["HTML_LAYOUT_RENDERER"] = renderer

    assert AppSettings.model_validate(local).html_layout_renderer == renderer
