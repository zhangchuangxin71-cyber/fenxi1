from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterable
from time import monotonic
from typing import Any

from pydantic import BaseModel, ValidationError

from app.agent_engine.contracts import RunContext
from app.agent_engine.errors import AgentEngineError, AgentToolError
from app.agent_engine.tools.base import ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise AgentEngineError("AGENT_TOOL_DUPLICATE", f"Duplicate tool: {spec.name}")
        if spec.handler is None:
            raise AgentEngineError("AGENT_TOOL_HANDLER_MISSING", f"Tool has no handler: {spec.name}")
        self._tools[spec.name] = spec

    def resolve(self, name: str, *, node_name: str, allowlist: set[str]) -> ToolSpec:
        if name not in allowlist:
            raise AgentToolError("AGENT_TOOL_NOT_ALLOWED", f"Tool is not allowed for this node: {name}")
        try:
            spec = self._tools[name]
        except KeyError as exc:
            raise AgentToolError("AGENT_TOOL_UNKNOWN", f"Unknown tool: {name}") from exc
        if node_name not in spec.allowed_nodes:
            raise AgentToolError("AGENT_TOOL_NODE_FORBIDDEN", f"Tool is forbidden for node: {name}")
        return spec

    def schemas(self, *, node_name: str, allowlist: Iterable[str]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for name in allowlist:
            spec = self.resolve(name, node_name=node_name, allowlist=set(allowlist))
            result.append(spec.provider_schema())
        return result

    async def execute(
        self,
        spec: ToolSpec,
        raw_arguments: dict[str, Any],
        context: RunContext,
        *,
        timeout_seconds: float,
    ) -> tuple[BaseModel, int]:
        try:
            arguments = spec.input_model.model_validate(raw_arguments)
        except ValidationError as exc:
            raise AgentToolError(
                "AGENT_TOOL_ARGUMENT_INVALID",
                f"Invalid arguments for tool {spec.name}.",
                {"validation_error": str(exc)[:2000]},
            ) from exc
        attempts = 0
        deadline = monotonic() + min(timeout_seconds, spec.timeout_seconds)
        while True:
            attempts += 1
            context.cancellation.raise_if_cancelled()
            try:
                handler = spec.handler
                if handler is None:
                    raise AgentToolError("AGENT_TOOL_HANDLER_MISSING", f"Tool has no handler: {spec.name}")
                result = handler(arguments, context)
                if inspect.isawaitable(result):
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"Tool {spec.name} timed out.")
                    operation = asyncio.ensure_future(result)
                    cancelled = asyncio.create_task(context.cancellation.wait())
                    try:
                        done, _ = await asyncio.wait(
                            {operation, cancelled},
                            timeout=remaining,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if cancelled in done:
                            operation.cancel()
                            await asyncio.gather(operation, return_exceptions=True)
                            raise asyncio.CancelledError
                        if operation not in done:
                            operation.cancel()
                            await asyncio.gather(operation, return_exceptions=True)
                            raise TimeoutError(f"Tool {spec.name} timed out.")
                        result = await operation
                    except asyncio.CancelledError:
                        operation.cancel()
                        await asyncio.gather(operation, return_exceptions=True)
                        raise
                    finally:
                        cancelled.cancel()
                        await asyncio.gather(cancelled, return_exceptions=True)
                return spec.output_model.model_validate(result), attempts
            except asyncio.CancelledError:
                raise
            except AgentEngineError:
                raise
            except Exception as exc:
                error_code = str(getattr(exc, "code", type(exc).__name__))
                retryable = bool(getattr(exc, "retryable", False)) or error_code in spec.retryable_error_codes
                if retryable and attempts <= spec.max_retries:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"Tool {spec.name} timed out.") from exc
                    await asyncio.sleep(min(0.25 * (2 ** (attempts - 1)), 2.0, remaining))
                    continue
                raise AgentToolError(
                    "AGENT_TOOL_EXECUTION_FAILED",
                    f"Tool {spec.name} failed.",
                    {"error_class": type(exc).__name__, "message": str(exc)[:2000]},
                ) from exc
