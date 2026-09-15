from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class StrictToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    allowed_nodes: frozenset[str]
    timeout_seconds: float = 60.0
    max_calls: int = 3
    max_retries: int = 0
    side_effect: Literal["read_only", "memory_only"] = "memory_only"
    retryable_error_codes: frozenset[str] = frozenset()
    semantic_argument_fields: tuple[str, ...] | None = None
    handler: Callable[[Any, Any], Awaitable[Any]] | None = None

    def provider_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": schema,
            "strict": True,
        }
