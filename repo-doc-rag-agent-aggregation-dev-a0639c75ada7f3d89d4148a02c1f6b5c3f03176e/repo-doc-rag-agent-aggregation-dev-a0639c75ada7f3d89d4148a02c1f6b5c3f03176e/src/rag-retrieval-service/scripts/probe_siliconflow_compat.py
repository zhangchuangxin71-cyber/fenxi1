#!/usr/bin/env python3
"""Probe SiliconFlow compatibility with this service's LLM contract.

The probe deliberately exercises both the public OpenAI-compatible API and the
project's existing ``OpenAICompatibleProvider``. It never prints the API key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.llm.gateway import LLMRequest  # noqa: E402
from app.llm.provider import OpenAICompatibleProvider  # noqa: E402

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_KEY_FILE = Path("/workspace/test/API_KEY")


@dataclass(slots=True)
class ProbeResult:
    name: str
    required: bool
    passed: bool
    detail: str


def load_api_key(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"API key file does not exist: {path}")
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == "API_KEY":
            key = value.strip().strip('"').strip("'")
            if key:
                return key
    raise ValueError(f"API_KEY is missing or empty in {path}")


def safe_error(exc: Exception, api_key: str) -> str:
    text = str(exc).replace(api_key, "<redacted>")
    text = re.sub(r"(?i)bearer\s+[a-z0-9._-]+", "Bearer <redacted>", text)
    text = " ".join(text.split())
    status = getattr(exc, "status_code", None)
    prefix = f"HTTP {status}: " if status is not None else ""
    return (prefix + text)[:1200]


def select_model(model_ids: list[str], explicit_model: str | None) -> str:
    if explicit_model:
        if explicit_model not in model_ids:
            raise ValueError(f"requested model is not present in /models: {explicit_model}")
        return explicit_model

    glm_ids = [model_id for model_id in model_ids if "glm" in model_id.casefold()]
    priorities = ("glm-5.2", "glm5.2", "glm-5", "glm5")
    for marker in priorities:
        matches = [model_id for model_id in glm_ids if marker in model_id.casefold()]
        if matches:
            return sorted(matches, key=lambda value: (not value.startswith("Pro/"), value))[0]
    available = ", ".join(sorted(glm_ids)[:20]) or "none"
    raise ValueError(f"no GLM-5.x model found; GLM models returned by /models: {available}")


def strict_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "retrieval_probe",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["routed_direct"]},
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                },
                "required": ["category", "queries"],
                "additionalProperties": False,
            },
        },
    }


def tool(name: str, description: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "document_name": {"type": "string"},
                },
                "required": ["document_name"],
                "additionalProperties": False,
            },
        },
    }


def finish_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "record_probe_result",
            "description": "Record that all supplied tool results were received.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"received": {"type": "boolean", "const": True}},
                "required": ["received"],
                "additionalProperties": False,
            },
        },
    }


def validate_strict_payload(data: Any) -> None:
    if not isinstance(data, dict) or set(data) != {"category", "queries"}:
        keys = sorted(data) if isinstance(data, dict) else None
        raise ValueError(f"schema mismatch: keys={keys}")
    if data["category"] != "routed_direct":
        raise ValueError(f"unexpected category: {data['category']!r}")
    if not isinstance(data["queries"], list) or not data["queries"]:
        raise ValueError("queries must be a non-empty array")
    if not all(isinstance(item, str) for item in data["queries"]):
        raise ValueError("queries must contain only strings")


def validate_tool_calls(message: Any, expected_names: set[str]) -> list[Any]:
    calls = list(message.tool_calls or [])
    names = {call.function.name for call in calls}
    if names != expected_names or len(calls) != len(expected_names):
        raise ValueError(f"expected tool calls {sorted(expected_names)}, got {sorted(names)}")
    for call in calls:
        arguments = json.loads(call.function.arguments)
        if set(arguments) != {"document_name"} or not isinstance(
            arguments["document_name"], str
        ):
            raise ValueError(f"strict arguments were not respected by {call.function.name}")
    return calls


async def run_probe(
    name: str,
    *,
    required: bool,
    operation: Callable[[], Awaitable[str]],
    api_key: str,
) -> ProbeResult:
    try:
        detail = await operation()
        result = ProbeResult(name=name, required=required, passed=True, detail=detail)
    except Exception as exc:  # This is a diagnostic boundary: every capability must be reported.
        result = ProbeResult(
            name=name,
            required=required,
            passed=False,
            detail=safe_error(exc, api_key),
        )
    status = "PASS" if result.passed else "FAIL"
    requirement = "required" if required else "diagnostic"
    print(f"[{status}] {name} ({requirement}): {result.detail}", flush=True)
    return result


async def main_async(args: argparse.Namespace) -> int:
    api_key = load_api_key(args.api_key_file)
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=args.base_url,
        timeout=args.timeout,
        max_retries=0,
    )
    provider = OpenAICompatibleProvider(
        api_key=api_key,
        base_url=args.base_url,
        timeout_seconds=args.timeout,
        max_retries=0,
    )
    results: list[ProbeResult] = []
    try:
        model_page = await client.models.list()
        model_ids = [item.id for item in model_page.data]
        model = select_model(model_ids, args.model)
        print(f"Selected model: {model}")
        print(f"Base URL: {args.base_url}")
        print("API key: <redacted>")

        async def standard_chat() -> str:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Reply with exactly: OK"}],
                max_tokens=128,
                temperature=0,
                extra_body={"enable_thinking": False},
            )
            if not response.choices or not (response.choices[0].message.content or "").strip():
                raise ValueError("completion contained no text")
            usage = response.usage
            if usage is None or int(usage.total_tokens or 0) <= 0:
                raise ValueError("standard OpenAI usage fields were absent")
            return f"chat.completions and usage fields accepted; total_tokens={usage.total_tokens}"

        results.append(
            await run_probe(
                "openai_chat_completions",
                required=True,
                operation=standard_chat,
                api_key=api_key,
            )
        )

        async def json_object_mode() -> str:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "Return one valid JSON object only."},
                    {"role": "user", "content": 'Return {"ok": true}.'},
                ],
                response_format={"type": "json_object"},
                max_tokens=128,
                temperature=0,
                extra_body={"enable_thinking": False},
            )
            parsed = json.loads(response.choices[0].message.content or "")
            if parsed != {"ok": True}:
                raise ValueError(f"unexpected JSON object: {parsed!r}")
            return "documented SiliconFlow json_object mode returned valid JSON"

        results.append(
            await run_probe(
                "json_object_mode",
                required=False,
                operation=json_object_mode,
                api_key=api_key,
            )
        )

        async def strict_json_schema_api() -> str:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": "Classify 'how many pages?' and return the required schema.",
                    }
                ],
                response_format=strict_schema(),
                max_tokens=256,
                temperature=0,
                extra_body={"enable_thinking": False},
            )
            data = json.loads(response.choices[0].message.content or "")
            validate_strict_payload(data)
            return "strict json_schema was accepted and the response validated"

        results.append(
            await run_probe(
                "strict_json_schema_api",
                required=True,
                operation=strict_json_schema_api,
                api_key=api_key,
            )
        )

        strict_tools = [
            tool("get_page_count", "Get a document's page count."),
            tool("get_chapter_count", "Get a document's chapter count."),
        ]
        first_tool_response: Any | None = None

        async def strict_parallel_tools() -> str:
            nonlocal first_tool_response
            first_tool_response = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Call get_page_count for Energy Law and get_chapter_count for "
                            "Company Report now. Call both tools in this single response."
                        ),
                    }
                ],
                tools=strict_tools,
                tool_choice="required",
                parallel_tool_calls=True,
                max_tokens=256,
                temperature=0,
                extra_body={"enable_thinking": False},
            )
            calls = validate_tool_calls(
                first_tool_response.choices[0].message,
                {"get_page_count", "get_chapter_count"},
            )
            return f"strict=true accepted; {len(calls)} parallel tool calls validated"

        parallel_result = await run_probe(
            "strict_parallel_function_calling",
            required=True,
            operation=strict_parallel_tools,
            api_key=api_key,
        )
        results.append(parallel_result)

        async def tool_history() -> str:
            if first_tool_response is None:
                raise ValueError("first tool-call response was unavailable")
            assistant = first_tool_response.choices[0].message.model_dump(exclude_none=True)
            messages: list[dict[str, Any]] = [
                {
                    "role": "user",
                    "content": (
                        "Call get_page_count for Energy Law and get_chapter_count for "
                        "Company Report now. Call both tools in this single response."
                    ),
                },
                assistant,
            ]
            for call in first_tool_response.choices[0].message.tool_calls or []:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps({"value": 12}, ensure_ascii=False),
                    }
                )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Acknowledge both tool results in one short sentence; call no tools."
                    ),
                }
            )
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=strict_tools,
                tool_choice="none",
                max_tokens=128,
                temperature=0,
                extra_body={"enable_thinking": False},
            )
            if not (response.choices[0].message.content or "").strip():
                raise ValueError("follow-up response contained no text")
            return "assistant tool_calls plus role=tool history was accepted"

        results.append(
            await run_probe(
                "tool_messages_in_history",
                required=True,
                operation=tool_history,
                api_key=api_key,
            )
        )

        async def project_structured_provider() -> str:
            result = await provider.complete_json(
                LLMRequest(
                    request_id="siliconflow-probe-structured",
                    phase="probe",
                    messages=[
                        {
                            "role": "user",
                            "content": "Classify 'how many pages?' and return the required schema.",
                        }
                    ],
                    model=model,
                    max_tokens=256,
                    response_format=strict_schema(),
                )
            )
            validate_strict_payload(result.data)
            return "existing provider's exact request shape succeeded"

        results.append(
            await run_probe(
                "project_provider_strict_schema",
                required=True,
                operation=project_structured_provider,
                api_key=api_key,
            )
        )

        async def project_tool_provider() -> str:
            initial_messages: list[dict[str, Any]] = [
                {
                    "role": "system",
                    "content": (
                        "First call get_page_count. After receiving its tool result, "
                        "call record_probe_result."
                    ),
                },
                {
                    "role": "user",
                    "content": "Check the page count for Energy Law.",
                },
            ]
            result = await provider.complete_json(
                LLMRequest(
                    request_id="siliconflow-probe-tools",
                    phase="probe",
                    messages=initial_messages,
                    model=model,
                    max_tokens=256,
                    tools=[strict_tools[0]],
                    tool_choice="required",
                    parallel_tool_calls=True,
                )
            )
            if len(result.tool_calls) != 1 or result.tool_calls[0].name != "get_page_count":
                raise ValueError("existing provider did not parse the expected tool call")
            arguments = result.tool_calls[0].arguments
            if set(arguments) != {"document_name"}:
                raise ValueError(f"existing provider received non-strict arguments: {arguments!r}")
            call = result.tool_calls[0]
            history = [
                *initial_messages,
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": json.dumps({"page_count": 12}),
                },
            ]
            follow_up = await provider.complete_json(
                LLMRequest(
                    request_id="siliconflow-probe-tools",
                    phase="probe-history",
                    messages=history,
                    model=model,
                    max_tokens=128,
                    tools=[finish_tool()],
                    tool_choice="required",
                    parallel_tool_calls=False,
                )
            )
            if len(follow_up.tool_calls) != 1:
                raise ValueError("existing provider did not parse the history follow-up call")
            final_call = follow_up.tool_calls[0]
            if final_call.name != "record_probe_result" or final_call.arguments != {
                "received": True
            }:
                raise ValueError(f"unexpected history follow-up: {final_call!r}")
            return "existing provider's strict tools, parser, and role=tool history succeeded"

        results.append(
            await run_probe(
                "project_provider_strict_tools_and_history",
                required=True,
                operation=project_tool_provider,
                api_key=api_key,
            )
        )
    finally:
        await client.close()
        await provider.close()

    required_failures = [result.name for result in results if result.required and not result.passed]
    report = {
        "base_url": args.base_url,
        "model": model,
        "env_only_compatible": not required_failures,
        "required_failures": required_failures,
        "results": [asdict(result) for result in results],
    }
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"JSON report written to: {args.json_output}")
    print(
        "Environment-only switch verdict: "
        + ("PASS" if report["env_only_compatible"] else "FAIL")
    )
    return 0 if report["env_only_compatible"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--model",
        help="Exact model ID; otherwise select GLM-5.2/GLM-5 from /models",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--json-output", type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main_async(parse_args())))
    except (ValueError, OSError) as exc:
        print(f"Probe setup failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
