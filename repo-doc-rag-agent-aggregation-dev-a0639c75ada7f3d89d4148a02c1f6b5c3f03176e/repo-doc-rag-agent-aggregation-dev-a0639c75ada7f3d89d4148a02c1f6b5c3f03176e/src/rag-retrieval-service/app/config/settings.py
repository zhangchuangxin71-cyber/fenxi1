from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration for one retrieval-service process.

    The service deliberately has no caller API-key settings.  ``user_id`` is the
    scope and rate-limit identity supplied by the already authenticated caller.
    """

    app_name: str = "rag-retrieval-service"
    app_env: str = "dev"
    app_version: str = "0.1.0"
    app_host: str = "127.0.0.1"
    app_port: int = 8120
    app_max_concurrency: int = 30
    app_max_queued_requests: int = 90
    app_admission_wait_timeout_seconds: float = 120.0
    log_level: str = "INFO"

    postgres_dsn: str = ""
    ingestion_service_base_url: str = "http://127.0.0.1:8100"
    ingestion_repair_timeout_seconds: float = 10.0
    db_pool_min: int = 1
    db_pool_max: int = 30
    db_connect_timeout_seconds: float = 5.0
    db_pool_acquire_timeout_seconds: float = 10.0
    db_query_timeout_seconds: float = 60.0
    db_pool_idle_ttl_seconds: float = 120.0
    db_pool_reaper_interval_seconds: float = 30.0

    rag_api_prefix: str = "/rag/v1"
    rag_default_top_k: int = 5
    rag_max_top_k: int = 100
    rag_max_doc_ids: int = 100
    rag_default_return_tokens: int = 8192
    rag_min_return_tokens: int = 128
    rag_max_return_tokens: int = 262_144
    rag_default_include_document_meta: bool = True
    rag_document_prefilter_threshold: float = 0.05
    rag_document_prefilter_max_candidates: int = 32
    rag_tree_max_steps: int = 4
    rag_tree_scan_max_tokens: int = 4096
    rag_page_accept_score: float = 20.0
    rag_page_possible_score: float = 1.0
    rag_minimum_page_tokens: int = 64
    rag_query_classification_strategy: Literal["fast", "robust"] = "robust"

    ark_api_key: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    rag_llm_model: str = ""
    rag_llm_context_window: int = 32768
    rag_llm_max_output_tokens: int = 2048
    rag_llm_safety_margin_tokens: int = 1024
    rag_llm_timeout_seconds: float = 90.0
    rag_llm_max_retries: int = 0
    rag_llm_max_concurrency: int = 16
    rag_llm_max_queued_calls: int = 1024
    rag_llm_per_request_max_in_flight: int = 6
    rag_llm_circuit_breaker_enabled: bool = True
    rag_llm_circuit_failure_threshold: int = 3
    rag_llm_circuit_recovery_seconds: float = 30.0

    request_soft_deadline_seconds: float = 600.0
    request_hard_deadline_seconds: float = 900.0
    request_finalization_reserve_seconds: float = 30.0
    rag_debug_enabled: bool = False

    rag_rate_limit_enabled: bool = True
    rag_rate_limit_per_minute: int = 1200
    rag_rate_limit_burst: int = 300
    rag_rate_limit_bucket_ttl_seconds: float = 900.0

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[2] / ".env"),
        env_file_encoding="utf-8-sig",
        env_prefix="",
        extra="ignore",
    )

    @field_validator("rag_api_prefix")
    @classmethod
    def normalize_prefix(cls, value: str) -> str:
        text = (value or "/rag/v1").strip()
        if not text.startswith("/"):
            text = "/" + text
        return text.rstrip("/") or "/rag/v1"

    @field_validator(
        "db_pool_min",
        "db_pool_max",
        "app_max_concurrency",
        "app_max_queued_requests",
        "rag_default_top_k",
        "rag_max_top_k",
        "rag_default_return_tokens",
        "rag_min_return_tokens",
        "rag_max_return_tokens",
        "rag_tree_max_steps",
        "rag_tree_scan_max_tokens",
        "rag_minimum_page_tokens",
        "rag_llm_context_window",
        "rag_llm_max_output_tokens",
        "rag_llm_safety_margin_tokens",
        "rag_llm_max_concurrency",
        "rag_llm_max_queued_calls",
        "rag_llm_per_request_max_in_flight",
        "rag_llm_circuit_failure_threshold",
        "rag_rate_limit_per_minute",
        "rag_rate_limit_burst",
        "rag_document_prefilter_max_candidates",
    )
    @classmethod
    def positive_int(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError("must be positive")
        return int(value)

    @field_validator(
        "rag_document_prefilter_threshold", "rag_page_accept_score", "rag_page_possible_score"
    )
    @classmethod
    def non_negative_float(cls, value: float) -> float:
        if float(value) < 0:
            raise ValueError("must not be negative")
        return float(value)

    @field_validator(
        "db_connect_timeout_seconds",
        "ingestion_repair_timeout_seconds",
        "db_pool_acquire_timeout_seconds",
        "db_query_timeout_seconds",
        "db_pool_idle_ttl_seconds",
        "db_pool_reaper_interval_seconds",
        "app_admission_wait_timeout_seconds",
        "rag_llm_timeout_seconds",
        "rag_llm_circuit_recovery_seconds",
        "request_soft_deadline_seconds",
        "request_hard_deadline_seconds",
        "request_finalization_reserve_seconds",
        "rag_rate_limit_bucket_ttl_seconds",
    )
    @classmethod
    def positive_float(cls, value: float) -> float:
        if float(value) <= 0:
            raise ValueError("must be positive")
        return float(value)

    @field_validator("rag_max_doc_ids")
    @classmethod
    def non_negative_doc_limit(cls, value: int) -> int:
        if int(value) < 0:
            raise ValueError("must not be negative")
        return int(value)

    @model_validator(mode="after")
    def validate_ranges(self) -> Settings:
        if (
            not self.rag_min_return_tokens
            <= self.rag_default_return_tokens
            <= self.rag_max_return_tokens
        ):
            raise ValueError("return token range must satisfy min <= default <= max")
        if self.rag_default_top_k > self.rag_max_top_k:
            raise ValueError("default top_k must not exceed max top_k")
        if (
            self.rag_llm_context_window
            <= self.rag_llm_max_output_tokens + self.rag_llm_safety_margin_tokens
        ):
            raise ValueError("LLM context window has no room for output and safety margin")
        if self.request_hard_deadline_seconds <= self.request_soft_deadline_seconds:
            raise ValueError("hard deadline must be greater than soft deadline")
        if self.request_finalization_reserve_seconds >= self.request_hard_deadline_seconds:
            raise ValueError("finalization reserve must be less than hard deadline")
        if (
            self.request_soft_deadline_seconds + self.request_finalization_reserve_seconds
            >= self.request_hard_deadline_seconds
        ):
            raise ValueError(
                "soft deadline plus finalization reserve must fit before hard deadline"
            )
        return self

    def normalized_pool_bounds(self) -> tuple[int, int]:
        return max(1, self.db_pool_min), max(max(1, self.db_pool_min), self.db_pool_max)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
