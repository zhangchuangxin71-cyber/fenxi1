from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest

from app.platform.errors import ApiError


@dataclass(frozen=True, slots=True)
class AuthContext:
    token: str
    fingerprint: str


class ApiKeyAuthenticator:
    def __init__(self, *, enabled: bool, allowed_keys: set[str]) -> None:
        self._enabled = enabled
        self._allowed_keys = allowed_keys

    def authenticate(self, authorization: str | None) -> AuthContext:
        if not self._enabled:
            token = self._extract_optional(authorization)
            return AuthContext(
                token=token,
                fingerprint=self._fingerprint(token) if token else "anonymous",
            )
        if not self._allowed_keys:
            raise ApiError(
                503,
                "auth_not_configured",
                "knowledge-chat API key authentication is enabled but no keys are configured",
                retryable=False,
            )
        token = self._extract_required(authorization)
        if not any(compare_digest(token, expected) for expected in self._allowed_keys):
            raise ApiError(401, "invalid_api_key", "invalid knowledge-chat API key")
        return AuthContext(token=token, fingerprint=self._fingerprint(token))

    @staticmethod
    def _extract_optional(authorization: str | None) -> str:
        text = (authorization or "").strip()
        if not text:
            return ""
        if not text.startswith("Bearer "):
            return ""
        return text.removeprefix("Bearer ").strip()

    @classmethod
    def _extract_required(cls, authorization: str | None) -> str:
        token = cls._extract_optional(authorization)
        if not token:
            raise ApiError(401, "invalid_api_key", "missing Bearer knowledge-chat API key")
        return token

    @staticmethod
    def _fingerprint(token: str) -> str:
        return sha256(token.encode("utf-8")).hexdigest()[:12]
