from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.chat.orchestrator import ChatOrchestrator
from app.platform.auth import ApiKeyAuthenticator
from app.platform.settings import Settings


@dataclass(slots=True)
class ServiceContainer:
    settings: Settings
    authenticator: ApiKeyAuthenticator
    request_limiter: Any
    ark_limiter: Any
    orchestrator: ChatOrchestrator
    closeables: tuple[Any, ...] = ()
