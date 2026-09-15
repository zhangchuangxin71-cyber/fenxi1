from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from app.agent_engine.errors import AgentContextExceeded


@dataclass(frozen=True, slots=True)
class StoredObject:
    object_id: str
    content_hash: str
    value: Any


class MemoryObjectStore:
    """Run-scoped canonical values; never persists or writes temporary files."""

    def __init__(self) -> None:
        self._values: dict[str, StoredObject] = {}

    def put(self, value: Any, *, prefix: str = "obj") -> StoredObject:
        item = StoredObject(
            object_id=f"{prefix}_{uuid4().hex}",
            content_hash=stable_content_hash(value),
            value=value,
        )
        self._values[item.object_id] = item
        return item

    def get(self, object_id: str, content_hash: str | None = None) -> StoredObject:
        try:
            item = self._values[object_id]
        except KeyError as exc:
            raise KeyError(f"Unknown run-scoped object: {object_id}") from exc
        if content_hash is not None and item.content_hash != content_hash:
            raise ValueError(f"Object hash mismatch: {object_id}")
        return item

    def clear(self) -> None:
        self._values.clear()

    def __len__(self) -> int:
        return len(self._values)


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    input_items: list[dict[str, Any]]
    estimated_tokens: int
    compressed: bool
    removed_items: int = 0


class ContextBudget:
    """Conservative preflight and deterministic compaction for Responses input."""

    def __init__(
        self,
        *,
        max_tokens: int,
        reserve_tokens: int = 8_192,
        recent_full_rounds: int = 3,
        max_compactions: int = 2,
    ) -> None:
        self.max_tokens = max_tokens
        self.reserve_tokens = min(max(0, reserve_tokens), max_tokens // 4)
        self.recent_full_rounds = max(1, recent_full_rounds)
        self.max_compactions = max(0, max_compactions)
        self._calibration_ratio = 1.0

    def preflight(
        self,
        input_items: list[dict[str, Any]],
        *,
        overhead: Any = None,
        compaction_count: int = 0,
    ) -> ContextSnapshot:
        estimated = self.estimate_tokens([input_items, overhead])
        if estimated + self.reserve_tokens <= self.max_tokens:
            return ContextSnapshot(list(input_items), estimated, False)
        if compaction_count >= self.max_compactions:
            raise AgentContextExceeded(
                "AGENT_CONTEXT_EXCEEDED",
                f"Context exceeds configured limit of {self.max_tokens} tokens.",
                {"estimated_tokens": estimated, "reserve_tokens": self.reserve_tokens},
            )
        compacted, removed = self._compact(input_items)
        estimated = self.estimate_tokens([compacted, overhead])
        if estimated + self.reserve_tokens > self.max_tokens:
            raise AgentContextExceeded(
                "AGENT_CONTEXT_EXCEEDED",
                (
                    f"Context needs about {estimated + self.reserve_tokens} tokens, exceeding the "
                    f"configured {self.max_tokens}."
                ),
                {"estimated_tokens": estimated, "reserve_tokens": self.reserve_tokens},
            )
        return ContextSnapshot(compacted, estimated, True, removed)

    def estimate_tokens(self, value: Any) -> int:
        serialized = stable_json(value)
        # Provider-independent and deliberately conservative for mixed Chinese/JSON.
        baseline = math.ceil(
            max(len(serialized.encode("utf-8")) / 3, len(serialized) / 2) * 1.2
        ) + 64
        return max(1, math.ceil(baseline * self._calibration_ratio))

    def calibrate(self, *, estimated_tokens: int, actual_tokens: int) -> None:
        if estimated_tokens <= 0 or actual_tokens <= estimated_tokens:
            return
        self._calibration_ratio = max(
            self._calibration_ratio,
            actual_tokens / estimated_tokens,
        )

    def _compact(self, input_items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        # Only complete provider function-call/output pairs are compactable. System
        # instructions, user corrections, validator errors and orphaned protocol
        # items stay byte-for-byte in their original position.
        pair_starts: list[int] = []
        for index in range(len(input_items) - 1):
            call = input_items[index]
            output = input_items[index + 1]
            if (
                call.get("type") == "function_call"
                and output.get("type") == "function_call_output"
                and call.get("call_id") == output.get("call_id")
                and _is_compactable_tool_output(output)
            ):
                pair_starts.append(index)

        keep_count = max(1, self.recent_full_rounds)
        old_starts = set(pair_starts[:-keep_count])
        if not old_starts:
            return list(input_items), 0

        compacted: list[dict[str, Any]] = []
        round_number = 0
        index = 0
        while index < len(input_items):
            if index in old_starts:
                summary_group: list[dict[str, Any]] = []
                while index in old_starts:
                    round_number += 1
                    summary_group.append(
                        _summarize_tool_round(
                            round_number,
                            [input_items[index]],
                            [input_items[index + 1]],
                        )
                    )
                    index += 2
                compacted.append(
                    {
                        "role": "assistant",
                        "content": "已完成的早期工具轮次摘要："
                        + stable_json(summary_group),
                    }
                )
                continue
            compacted.append(input_items[index])
            index += 1
        return compacted, len(old_starts)


def stable_fingerprint(name: str, arguments: Any, error_class: str) -> str:
    raw = stable_json({"tool": name, "arguments": arguments, "error": error_class})
    return hashlib.sha256(raw.encode()).hexdigest()


def stable_content_hash(value: Any) -> str:
    canonical = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return hashlib.sha256(stable_json(canonical).encode()).hexdigest()


def bounded_tool_output(
    value: Any,
    *,
    max_chars: int,
    objects: MemoryObjectStore,
) -> tuple[str, str | None]:
    serialized = stable_json(value)
    if len(serialized) <= max_chars:
        return serialized, None
    stored = objects.put(value, prefix="tool_result")
    envelope = {
        "truncated": True,
        "object_id": stored.object_id,
        "content_hash": stored.content_hash,
        "preview": serialized[:max_chars],
    }
    return stable_json(envelope), stored.object_id


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "...[truncated]"


def _summarize_tool_round(
    round_number: int,
    calls: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    for item in outputs:
        raw_output = str(item.get("output") or "")
        parsed: Any = None
        try:
            parsed = json.loads(raw_output)
        except (TypeError, json.JSONDecodeError):
            pass
        observation: dict[str, Any] = {
            "success": not (isinstance(parsed, dict) and parsed.get("ok") is False),
            "short_observation": truncate(raw_output, 32),
        }
        if isinstance(parsed, dict) and parsed.get("object_id"):
            observation["object_id"] = str(parsed["object_id"])
        observations.append(observation)
    return {
        "round": round_number,
        "tools": [str(item.get("name") or "unknown") for item in calls],
        "observations": observations,
    }


def _is_compactable_tool_output(item: dict[str, Any]) -> bool:
    try:
        parsed = json.loads(str(item.get("output") or ""))
    except (TypeError, json.JSONDecodeError):
        return True
    if not isinstance(parsed, dict):
        return True
    if parsed.get("ok") is False or parsed.get("valid") is False:
        return False
    errors = parsed.get("errors")
    return not isinstance(errors, list) or not errors
