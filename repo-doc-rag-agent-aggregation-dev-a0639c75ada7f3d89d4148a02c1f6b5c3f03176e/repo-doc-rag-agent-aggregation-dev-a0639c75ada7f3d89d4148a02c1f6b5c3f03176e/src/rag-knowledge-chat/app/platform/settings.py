from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: Literal["dev", "prod", "test"] = Field(default="dev", alias="APP_ENV")
    debug_enabled: bool = Field(default=False, alias="DEBUG_ENABLED")
    auth_enabled: bool = Field(default=False, alias="AUTH_ENABLED")
    knowledge_chat_inbound_api_keys: str = Field(
        default="",
        alias="KNOWLEDGE_CHAT_INBOUND_API_KEYS",
    )

    ark_api_key: str = Field(default="", alias="ARK_API_KEY")
    ark_base_url: str = Field(default="https://ark.cn-beijing.volces.com/api/v3", alias="ARK_BASE_URL")
    model_smart: str = Field(default="doubao-seed-2-0-lite-260215", alias="MODEL_SMART")
    doubao_thinking_type: Literal["enabled", "disabled", "auto"] = Field(
        default="enabled", alias="DOUBAO_THINKING_TYPE"
    )

    rag_retrieval_service_url: str = Field(default="http://127.0.0.1:8120", alias="RAG_RETRIEVAL_SERVICE_URL")
    rag_retrieval_timeout_seconds: float = Field(
        default=180.0,
        gt=0,
        alias="RAG_RETRIEVAL_TIMEOUT_SECONDS",
    )
    rag_max_return_tokens: int = Field(
        default=180_000,
        ge=4_096,
        alias="RAG_MAX_RETURN_TOKENS",
    )

    request_rpm_limit: int = Field(default=30, ge=1, alias="REQUEST_RPM_LIMIT")
    ark_rpm_limit: int = Field(default=50, ge=1, alias="ARK_RPM_LIMIT")
    ark_max_concurrency: int = Field(default=4, ge=1, le=32, alias="ARK_MAX_CONCURRENCY")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    @property
    def retrieval_url(self) -> str:
        return self.rag_retrieval_service_url.rstrip("/")

    @model_validator(mode="after")
    def validate_inbound_auth_configuration(self) -> Settings:
        raw = self.knowledge_chat_inbound_api_keys.strip()
        if self.auth_enabled and not raw:
            raise ValueError("KNOWLEDGE_CHAT_INBOUND_API_KEYS is required when AUTH_ENABLED=true")
        if raw:
            self._parse_inbound_api_keys(raw)
        return self

    @staticmethod
    def _parse_inbound_api_keys(raw: str) -> tuple[str, ...]:
        tokens: list[str] = []
        for item in raw.split(","):
            value = item.strip()
            if not value or ":" not in value:
                raise ValueError("KNOWLEDGE_CHAT_INBOUND_API_KEYS must use caller:key entries")
            caller, token = (part.strip() for part in value.split(":", 1))
            if (
                not caller
                or not token
                or any(char.isspace() for char in caller)
                or any(char.isspace() for char in token)
            ):
                raise ValueError(
                    "KNOWLEDGE_CHAT_INBOUND_API_KEYS must use non-empty caller:key entries without whitespace"
                )
            tokens.append(token)
        return tuple(tokens)

    @property
    def allowed_api_keys(self) -> set[str]:
        raw = self.knowledge_chat_inbound_api_keys.strip()
        if not raw:
            return set()
        return set(self._parse_inbound_api_keys(raw))

    @property
    def default_inbound_api_key(self) -> str:
        raw = self.knowledge_chat_inbound_api_keys.strip()
        if not raw:
            return ""
        return self._parse_inbound_api_keys(raw)[0]


CHAT_MODEL_ALIAS = "rag-knowledge-chat"
REQUEST_DEADLINE_SECONDS = 90.0
REQUEST_FINALIZATION_RESERVE_SECONDS = 15.0
ARK_TIMEOUT_SECONDS = 60.0
MAX_REQUEST_BYTES = 256 * 1024
MAX_MESSAGE_CHARS = 120_000
MAX_RETRIEVAL_QUERY_CHARS = 2_000
