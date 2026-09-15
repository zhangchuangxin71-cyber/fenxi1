from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "rag-agent"
    app_env: Literal["dev", "prod", "test"] = "dev"
    host: str = "0.0.0.0"
    port: int = 8100
    log_level: str = "INFO"

    pg_dsn: str = "postgresql://postgres:change_me@127.0.0.1:5432/pageindex"

    ark_api_key: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    model_mini: str = "doubao-seed-2-0-lite-260215"
    model_fast: str = "doubao-seed-2-0-lite-260215"
    model_smart: str = "doubao-seed-2-0-lite-260215"
    doubao_thinking_type: Literal["enabled", "disabled", "auto"] = "enabled"
    doubao_reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = "medium"

    oss_enabled: bool = False
    oss_endpoint: str = ""
    oss_access_key: str = ""
    oss_secret_key: str = ""
    oss_bucket: str = ""
    oss_log_prefix: str = "rag-agent/logs/"

    llm_timeout_seconds: int = 60
    llm_max_retries: int = 2

    rag_top_k: int = 5
    rag_search_mode: Literal["hybrid", "keyword", "semantic"] = "hybrid"
    rag_max_candidates: int = 500
    doc_router_max_docs: int = 50

    report_image_enabled: bool = False
    report_image_max_sections: int = 3
    report_image_top_k: int = 3
    image_mcp_url: str = ""
    image_mcp_api_type: Literal["generic", "media_resources"] = "generic"
    image_mcp_api_key: str = ""
    image_mcp_tenant_code: str = ""
    image_mcp_timeout_seconds: int = 20

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")


settings = Settings()
