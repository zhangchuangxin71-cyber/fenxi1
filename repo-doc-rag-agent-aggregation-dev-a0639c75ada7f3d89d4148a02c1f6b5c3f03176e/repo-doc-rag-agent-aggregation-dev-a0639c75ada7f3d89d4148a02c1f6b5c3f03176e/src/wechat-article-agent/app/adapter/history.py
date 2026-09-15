from __future__ import annotations

import json
from typing import Any

_ARTIFACT_TYPES = {"agent.artifact", "function_call", "function_call_output", "tool_result"}


def compact_history(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    compact: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role", ""))
        content = message.get("content", "")
        text = _compact_content(content)
        if role in {"user", "assistant"} and text:
            compact.append({"role": role, "content": text})
    return compact


def _compact_content(content: Any) -> str:
    if isinstance(content, str):
        if len(content) > 20_000 and _looks_like_artifact(content):
            return "[历史产物正文已省略]"
        return content.strip()
    if not isinstance(content, list):
        return ""
    output: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") in _ARTIFACT_TYPES:
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            output.append(text.strip())
    return "\n".join(output)


def _looks_like_artifact(text: str) -> bool:
    sample = text[:1000].lower()
    return "<!doctype html" in sample or "<html" in sample or sample.count("#") >= 8


def history_as_json(messages: list[dict[str, str]]) -> str:
    return json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
