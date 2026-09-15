from app.platform.settings import Settings


def test_settings_defaults_to_disabled_inbound_auth() -> None:
    settings = Settings(
        _env_file=None,
        ARK_API_KEY="ark-secret",
        ARK_BASE_URL="https://ark.example/v3",
        MODEL_SMART="model-smart",
        RAG_RETRIEVAL_SERVICE_URL="http://retrieval:8120",
    )

    assert settings.ark_api_key == "ark-secret"
    assert settings.ark_base_url == "https://ark.example/v3"
    assert settings.model_smart == "model-smart"
    assert settings.retrieval_url == "http://retrieval:8120"
    assert settings.rag_retrieval_timeout_seconds == 180.0
    assert settings.rag_max_return_tokens == 180_000
    assert settings.auth_enabled is False
    assert settings.allowed_api_keys == set()


def test_explicit_knowledge_chat_inbound_api_keys_support_caller_prefixes() -> None:
    settings = Settings(
        _env_file=None,
        AUTH_ENABLED=True,
        KNOWLEDGE_CHAT_INBOUND_API_KEYS="frontend:key-a,qa:key-b",
    )

    assert settings.allowed_api_keys == {"key-a", "key-b"}


def test_enabled_inbound_auth_requires_well_formed_keys() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="KNOWLEDGE_CHAT_INBOUND_API_KEYS"):
        Settings(_env_file=None, AUTH_ENABLED=True, KNOWLEDGE_CHAT_INBOUND_API_KEYS="")
    with pytest.raises(ValidationError, match="caller:key"):
        Settings(
            _env_file=None,
            AUTH_ENABLED=True,
            KNOWLEDGE_CHAT_INBOUND_API_KEYS="missing-caller-prefix",
        )


def test_retrieval_timeout_is_configurable_from_environment_contract() -> None:
    settings = Settings(_env_file=None, RAG_RETRIEVAL_TIMEOUT_SECONDS=245)

    assert settings.rag_retrieval_timeout_seconds == 245.0


def test_rag_return_token_limit_is_configurable_from_environment_contract() -> None:
    settings = Settings(_env_file=None, RAG_MAX_RETURN_TOKENS=200_000)

    assert settings.rag_max_return_tokens == 200_000


def test_rag_return_token_limit_cannot_be_lower_than_dynamic_minimum() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, RAG_MAX_RETURN_TOKENS=4_095)
