from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AgentEngineError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


class AgentBudgetExceeded(AgentEngineError):
    pass


class AgentContextExceeded(AgentEngineError):
    pass


class AgentRepeatedFailure(AgentEngineError):
    pass


class AgentToolError(AgentEngineError):
    pass
