from __future__ import annotations

from functools import lru_cache
from typing import Literal
from uuid import UUID

from pydantic import Field, HttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    app_env: Literal["development", "test", "production"] = Field("development", alias="APP_ENV")
    app_host: str = Field("127.0.0.1", alias="APP_HOST")
    app_port: int = Field(8240, ge=1, le=65535, alias="APP_PORT")
    debug_enabled: bool = Field(False, alias="DEBUG_ENABLED")
    debug_trace_max_bytes: int = Field(8_388_608, ge=65_536, alias="DEBUG_TRACE_MAX_BYTES")
    debug_trace_value_max_chars: int = Field(1_000_000, ge=1_024, alias="DEBUG_TRACE_VALUE_MAX_CHARS")

    app_max_active_runs: int = Field(8, ge=1, le=512, alias="APP_MAX_ACTIVE_RUNS")
    app_max_queued_runs: int = Field(24, ge=0, le=10_000, alias="APP_MAX_QUEUED_RUNS")
    app_admission_wait_timeout_seconds: float = Field(120, gt=0, alias="APP_ADMISSION_WAIT_TIMEOUT_SECONDS")

    database_url: str = Field(alias="DATABASE_URL")
    db_pool_min: int = Field(1, ge=1, alias="DB_POOL_MIN")
    db_pool_max: int = Field(10, ge=1, alias="DB_POOL_MAX")
    db_connect_timeout_seconds: float = Field(5, gt=0, alias="DB_CONNECT_TIMEOUT_SECONDS")
    db_pool_acquire_timeout_seconds: float = Field(10, gt=0, alias="DB_POOL_ACQUIRE_TIMEOUT_SECONDS")
    db_query_timeout_seconds: float = Field(60, gt=0, alias="DB_QUERY_TIMEOUT_SECONDS")
    db_max_retries: int = Field(2, ge=0, le=5, alias="DB_MAX_RETRIES")

    cancel_confirm_timeout_seconds: float = Field(10, gt=0, alias="CANCEL_CONFIRM_TIMEOUT_SECONDS")
    cancel_reconcile_interval_seconds: float = Field(15, gt=0, alias="CANCEL_RECONCILE_INTERVAL_SECONDS")
    cancel_reconcile_batch_size: int = Field(20, ge=1, le=500, alias="CANCEL_RECONCILE_BATCH_SIZE")
    wechat_agent_thread_namespace: UUID = Field(alias="WECHAT_AGENT_THREAD_NAMESPACE")
    agent_server_url: HttpUrl = Field(alias="AGENT_SERVER_URL")
    agent_server_assistant_id: str = Field("wechat_article", alias="AGENT_SERVER_ASSISTANT_ID")
    artifact_ttl_hours: int = Field(72, ge=1, le=720, alias="ARTIFACT_TTL_HOURS")
    ttl_cleanup_interval_seconds: float = Field(300, gt=0, alias="TTL_CLEANUP_INTERVAL_SECONDS")
    ttl_cleanup_batch_size: int = Field(50, ge=1, le=500, alias="TTL_CLEANUP_BATCH_SIZE")

    ark_api_key: str = Field("", alias="ARK_API_KEY")
    ark_responses_url: HttpUrl = Field(alias="ARK_RESPONSES_URL")
    ark_model_fast: str = Field(alias="ARK_MODEL_FAST")
    ark_model_main: str = Field(alias="ARK_MODEL_MAIN")
    ark_context_window: int = Field(262_144, ge=8_192, alias="ARK_CONTEXT_WINDOW")
    ark_max_concurrency: int = Field(8, ge=1, alias="ARK_MAX_CONCURRENCY")
    ark_max_queued_calls: int = Field(180, ge=1, alias="ARK_MAX_QUEUED_CALLS")
    ark_per_run_max_in_flight: int = Field(6, ge=1, alias="ARK_PER_RUN_MAX_IN_FLIGHT")
    ark_queue_wait_timeout_seconds: float = Field(120, gt=0, alias="ARK_QUEUE_WAIT_TIMEOUT_SECONDS")
    ark_connect_timeout_seconds: float = Field(10, gt=0, alias="ARK_CONNECT_TIMEOUT_SECONDS")
    ark_first_event_timeout_seconds: float = Field(60, gt=0, alias="ARK_FIRST_EVENT_TIMEOUT_SECONDS")
    ark_stream_idle_timeout_seconds: float = Field(90, gt=0, alias="ARK_STREAM_IDLE_TIMEOUT_SECONDS")
    ark_call_max_seconds: float = Field(600, gt=0, alias="ARK_CALL_MAX_SECONDS")
    ark_max_retries: int = Field(2, ge=0, le=5, alias="ARK_MAX_RETRIES")
    ark_retry_base_seconds: float = Field(1, gt=0, alias="ARK_RETRY_BASE_SECONDS")
    ark_structured_output_max_repairs: int = Field(1, ge=0, le=3, alias="ARK_STRUCTURED_OUTPUT_MAX_REPAIRS")
    ark_structured_output_max_retries: int = Field(2, ge=0, le=5, alias="ARK_STRUCTURED_OUTPUT_MAX_RETRIES")
    ark_circuit_breaker_enabled: bool = Field(True, alias="ARK_CIRCUIT_BREAKER_ENABLED")
    ark_circuit_failure_threshold: int = Field(3, ge=1, alias="ARK_CIRCUIT_FAILURE_THRESHOLD")
    ark_circuit_recovery_seconds: float = Field(30, gt=0, alias="ARK_CIRCUIT_RECOVERY_SECONDS")
    ark_rate_limit_per_minute: int = Field(3000, ge=1, alias="ARK_RATE_LIMIT_PER_MINUTE")
    ark_rate_limit_burst: int = Field(600, ge=1, alias="ARK_RATE_LIMIT_BURST")
    ark_context_management_enabled: bool = Field(False, alias="ARK_CONTEXT_MANAGEMENT_ENABLED")
    ark_caching_enabled: bool = Field(False, alias="ARK_CACHING_ENABLED")
    ark_thinking_task_spec: bool = Field(False, alias="ARK_THINKING_TASK_SPEC")
    ark_thinking_outline: bool = Field(False, alias="ARK_THINKING_OUTLINE")
    ark_thinking_article: bool = Field(False, alias="ARK_THINKING_ARTICLE")
    web_search_enabled: bool = Field(False, alias="WEB_SEARCH_ENABLED")

    request_rate_limit_per_minute: int = Field(1200, ge=1, alias="REQUEST_RATE_LIMIT_PER_MINUTE")
    request_rate_limit_burst: int = Field(300, ge=1, alias="REQUEST_RATE_LIMIT_BURST")
    request_rate_limit_bucket_ttl_seconds: float = Field(
        900, gt=0, alias="REQUEST_RATE_LIMIT_BUCKET_TTL_SECONDS"
    )

    retrieval_base_url: HttpUrl = Field(alias="RETRIEVAL_BASE_URL")
    retrieval_connect_timeout_seconds: float = Field(10, gt=0, alias="RETRIEVAL_CONNECT_TIMEOUT_SECONDS")
    retrieval_timeout_seconds: float = Field(180, gt=0, alias="RETRIEVAL_TIMEOUT_SECONDS")
    retrieval_max_concurrency: int = Field(8, ge=1, alias="RETRIEVAL_MAX_CONCURRENCY")
    retrieval_max_retries: int = Field(2, ge=0, le=5, alias="RETRIEVAL_MAX_RETRIES")
    retrieval_retry_base_seconds: float = Field(1, gt=0, alias="RETRIEVAL_RETRY_BASE_SECONDS")
    retrieval_circuit_breaker_enabled: bool = Field(True, alias="RETRIEVAL_CIRCUIT_BREAKER_ENABLED")
    retrieval_circuit_failure_threshold: int = Field(3, ge=1, alias="RETRIEVAL_CIRCUIT_FAILURE_THRESHOLD")
    retrieval_circuit_recovery_seconds: float = Field(30, gt=0, alias="RETRIEVAL_CIRCUIT_RECOVERY_SECONDS")
    raw_repair_max_wait_seconds: float = Field(600, gt=0, alias="RAW_REPAIR_MAX_WAIT_SECONDS")
    short_document_max_chars: int = Field(80_000, ge=1_000, alias="SHORT_DOCUMENT_MAX_CHARS")
    material_query_groups_per_document: int = Field(
        3, ge=1, le=10, alias="MATERIAL_QUERY_GROUPS_PER_DOCUMENT"
    )
    article_max_retrieval_rounds: int = Field(3, ge=0, le=10, alias="ARTICLE_MAX_RETRIEVAL_ROUNDS")
    article_max_web_search_rounds: int = Field(2, ge=0, le=10, alias="ARTICLE_MAX_WEB_SEARCH_ROUNDS")
    clarification_max_rounds: int = Field(3, ge=1, le=10, alias="CLARIFICATION_MAX_ROUNDS")
    seedream_responses_url: HttpUrl = Field(alias="SEEDREAM_RESPONSES_URL")
    seedream_model: str = Field(alias="SEEDREAM_MODEL")
    seedream_size: str = Field("2K", alias="SEEDREAM_SIZE")
    seedream_max_concurrency: int = Field(4, ge=1, alias="SEEDREAM_MAX_CONCURRENCY")
    seedream_connect_timeout_seconds: float = Field(10, gt=0, alias="SEEDREAM_CONNECT_TIMEOUT_SECONDS")
    seedream_call_max_seconds: float = Field(600, gt=0, alias="SEEDREAM_CALL_MAX_SECONDS")
    seedream_max_retries: int = Field(2, ge=0, le=5, alias="SEEDREAM_MAX_RETRIES")
    seedream_retry_base_seconds: float = Field(1, gt=0, alias="SEEDREAM_RETRY_BASE_SECONDS")
    seedream_circuit_breaker_enabled: bool = Field(True, alias="SEEDREAM_CIRCUIT_BREAKER_ENABLED")
    seedream_circuit_failure_threshold: int = Field(3, ge=1, alias="SEEDREAM_CIRCUIT_FAILURE_THRESHOLD")
    seedream_circuit_recovery_seconds: float = Field(30, gt=0, alias="SEEDREAM_CIRCUIT_RECOVERY_SECONDS")
    image_max_count: int = Field(6, ge=0, le=20, alias="IMAGE_MAX_COUNT")
    html_layout_renderer: Literal["deterministic", "llm_decide", "skill_driven"] = Field(
        "skill_driven", alias="HTML_LAYOUT_RENDERER"
    )
    html_layout_default_theme: str = Field("professional-clean", alias="HTML_LAYOUT_DEFAULT_THEME")
    agent_engine_max_steps: int = Field(12, ge=1, le=100, alias="AGENT_ENGINE_MAX_STEPS")
    agent_engine_max_tool_calls: int = Field(16, ge=1, le=200, alias="AGENT_ENGINE_MAX_TOOL_CALLS")
    agent_engine_max_context_tokens: int = Field(131_072, ge=2_048, alias="AGENT_ENGINE_MAX_CONTEXT_TOKENS")
    agent_engine_context_reserve_tokens: int = Field(8_192, ge=0, alias="AGENT_ENGINE_CONTEXT_RESERVE_TOKENS")
    agent_engine_recent_full_rounds: int = Field(3, ge=1, le=20, alias="AGENT_ENGINE_RECENT_FULL_ROUNDS")
    agent_engine_max_compactions: int = Field(2, ge=0, le=10, alias="AGENT_ENGINE_MAX_COMPACTIONS")
    agent_engine_call_timeout_seconds: float = Field(120, gt=0, alias="AGENT_ENGINE_CALL_TIMEOUT_SECONDS")
    agent_engine_total_timeout_seconds: float = Field(300, gt=0, alias="AGENT_ENGINE_TOTAL_TIMEOUT_SECONDS")
    agent_engine_max_same_tool_failures: int = Field(
        2, ge=1, le=20, alias="AGENT_ENGINE_MAX_SAME_TOOL_FAILURES"
    )
    agent_engine_max_tool_result_chars: int = Field(
        24_000, ge=256, alias="AGENT_ENGINE_MAX_TOOL_RESULT_CHARS"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    @model_validator(mode="after")
    def validate_capacity(self) -> AppSettings:
        if self.db_pool_min > self.db_pool_max:
            raise ValueError("DB_POOL_MIN must not exceed DB_POOL_MAX")
        if self.app_max_queued_runs and self.app_max_queued_runs < self.app_max_active_runs:
            raise ValueError("APP_MAX_QUEUED_RUNS must be zero or at least APP_MAX_ACTIVE_RUNS")
        if self.ark_rate_limit_burst > self.ark_rate_limit_per_minute:
            raise ValueError("ARK_RATE_LIMIT_BURST must not exceed ARK_RATE_LIMIT_PER_MINUTE")
        if self.request_rate_limit_burst > self.request_rate_limit_per_minute:
            raise ValueError("REQUEST_RATE_LIMIT_BURST must not exceed REQUEST_RATE_LIMIT_PER_MINUTE")
        if self.agent_engine_max_context_tokens > self.ark_context_window:
            raise ValueError("AGENT_ENGINE_MAX_CONTEXT_TOKENS must not exceed ARK_CONTEXT_WINDOW")
        if self.agent_engine_context_reserve_tokens >= self.agent_engine_max_context_tokens:
            raise ValueError(
                "AGENT_ENGINE_CONTEXT_RESERVE_TOKENS must be smaller than AGENT_ENGINE_MAX_CONTEXT_TOKENS"
            )
        if self.agent_engine_call_timeout_seconds > self.agent_engine_total_timeout_seconds:
            raise ValueError(
                "AGENT_ENGINE_CALL_TIMEOUT_SECONDS must not exceed AGENT_ENGINE_TOTAL_TIMEOUT_SECONDS"
            )
        from app.rendering.wechat_layout.errors import ThemeRegistryError
        from app.rendering.wechat_layout.themes.registry import default_registry

        try:
            default_registry().get(self.html_layout_default_theme)
        except ThemeRegistryError as exc:
            raise ValueError("HTML_LAYOUT_DEFAULT_THEME must be a registered theme ID") from exc
        return self

    @property
    def thinking_phases(self) -> frozenset[str]:
        values = {
            "task_spec": self.ark_thinking_task_spec,
            "outline": self.ark_thinking_outline,
            "article": self.ark_thinking_article,
        }
        return frozenset(name for name, enabled in values.items() if enabled)


class DevPanelSettings(BaseSettings):
    dev_panel_host: str = Field("127.0.0.1", alias="DEV_PANEL_HOST")
    dev_panel_port: int = Field(8245, ge=1, le=65535, alias="DEV_PANEL_PORT")
    dev_retrieval_base_url: HttpUrl = Field(alias="DEV_RETRIEVAL_BASE_URL")
    dev_dataset_manifest_file: str = Field(alias="DEV_DATASET_MANIFEST_FILE")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)


@lru_cache
def get_settings() -> AppSettings:
    return AppSettings()  # type: ignore[call-arg]
