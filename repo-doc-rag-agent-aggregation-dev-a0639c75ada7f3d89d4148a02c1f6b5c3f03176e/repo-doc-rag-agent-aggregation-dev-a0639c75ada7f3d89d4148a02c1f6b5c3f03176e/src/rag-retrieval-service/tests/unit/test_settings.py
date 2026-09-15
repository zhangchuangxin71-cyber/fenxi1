from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.settings import Settings


def test_settings_normalize_runtime_bounds_without_auth_configuration() -> None:
    settings = Settings(
        _env_file=None,
        rag_api_prefix="rag/v1/",
        db_pool_min=3,
        db_pool_max=2,
        rag_default_return_tokens=4096,
        rag_min_return_tokens=256,
        rag_max_return_tokens=8192,
    )

    assert settings.rag_api_prefix == "/rag/v1"
    assert settings.normalized_pool_bounds() == (3, 3)
    assert not hasattr(settings, "rag_auth_enabled")
    assert not hasattr(settings, "rag_service_api_keys")


def test_settings_reject_invalid_deadline_or_token_ranges() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            request_soft_deadline_seconds=20,
            request_hard_deadline_seconds=10,
            request_finalization_reserve_seconds=2,
        )

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            rag_min_return_tokens=1000,
            rag_default_return_tokens=500,
            rag_max_return_tokens=2000,
        )


def test_settings_reject_llm_prompt_budget_without_safety_room() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            rag_llm_context_window=2048,
            rag_llm_max_output_tokens=1500,
            rag_llm_safety_margin_tokens=600,
        )


def test_production_defaults_bound_single_process_without_langsmith_configuration() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_max_concurrency == 30
    assert settings.app_max_queued_requests == 90
    assert settings.app_admission_wait_timeout_seconds == 120
    assert settings.db_pool_max == 30
    assert settings.db_pool_idle_ttl_seconds == 120
    assert settings.db_pool_reaper_interval_seconds == 30
    assert settings.rag_document_prefilter_max_candidates == 32
    assert settings.rag_max_return_tokens == 262_144
    assert settings.rag_llm_max_concurrency == 16
    assert settings.rag_llm_per_request_max_in_flight == 6
    assert settings.request_soft_deadline_seconds == 600
    assert settings.request_hard_deadline_seconds == 900
    assert settings.rag_rate_limit_per_minute == 1200
    assert settings.rag_rate_limit_burst == 300
    assert settings.rag_query_classification_strategy == "robust"
    assert not hasattr(settings, "langsmith_tracing_enabled")
    assert not hasattr(settings, "langsmith_project")


def test_classification_strategy_setting_accepts_only_fast_or_robust() -> None:
    settings = Settings(_env_file=None, rag_query_classification_strategy="fast")
    assert settings.rag_query_classification_strategy == "fast"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rag_query_classification_strategy="other")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("app_max_queued_requests", 0),
        ("app_admission_wait_timeout_seconds", 0),
        ("db_pool_idle_ttl_seconds", 0),
        ("db_pool_reaper_interval_seconds", 0),
        ("rag_document_prefilter_max_candidates", 0),
    ],
)
def test_capacity_guard_settings_must_be_positive(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
