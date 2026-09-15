import pytest

from app.platform.auth import ApiKeyAuthenticator
from app.platform.errors import ApiError


def test_valid_bearer_key_is_returned_for_retrieval_forwarding() -> None:
    auth = ApiKeyAuthenticator(enabled=True, allowed_keys={"secret"})

    context = auth.authenticate("Bearer secret")

    assert context.token == "secret"
    assert len(context.fingerprint) == 12


@pytest.mark.parametrize("header", [None, "", "secret", "Bearer wrong"])
def test_invalid_authorization_is_rejected(header) -> None:
    auth = ApiKeyAuthenticator(enabled=True, allowed_keys={"secret"})

    with pytest.raises(ApiError) as exc_info:
        auth.authenticate(header)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "invalid_api_key"


def test_disabled_auth_accepts_optional_token() -> None:
    context = ApiKeyAuthenticator(enabled=False, allowed_keys=set()).authenticate(None)

    assert context.token == ""
    assert context.fingerprint == "anonymous"
