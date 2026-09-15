from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.agent_engine.contracts import AgentBudget, CompletionValidator
from app.agent_engine.errors import AgentEngineError


@dataclass(frozen=True, slots=True)
class NodeAgentProfile:
    node_name: str
    completion_model: type[BaseModel]
    skill_allowlist: tuple[str, ...]
    tool_allowlist: tuple[str, ...]
    budget: AgentBudget
    completion_validator: CompletionValidator | None = None


class NodeProfileRegistry:
    """Fail-closed registry for node-specific Engine capabilities."""

    def __init__(self) -> None:
        self._profiles: dict[str, NodeAgentProfile] = {}

    def register(self, profile: NodeAgentProfile) -> None:
        if profile.node_name in self._profiles:
            raise AgentEngineError(
                "AGENT_PROFILE_DUPLICATE", f"Duplicate node Agent profile: {profile.node_name}"
            )
        self._profiles[profile.node_name] = profile

    def get(self, node_name: str) -> NodeAgentProfile:
        try:
            return self._profiles[node_name]
        except KeyError as exc:
            raise AgentEngineError(
                "AGENT_PROFILE_UNKNOWN", f"No Agent profile is registered for node: {node_name}"
            ) from exc

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "skills": profile.skill_allowlist,
                "tools": profile.tool_allowlist,
                "completion_model": profile.completion_model.__name__,
            }
            for name, profile in self._profiles.items()
        }
