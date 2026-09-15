from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.agent_engine import AgentBudget, AgentRequest, AgentRunner, ModelTurn, ToolCall
from app.agent_engine.context import ContextBudget, MemoryObjectStore
from app.agent_engine.contracts import CancellationToken, RunContext
from app.agent_engine.errors import AgentEngineError, AgentToolError
from app.agent_engine.registry import NodeAgentProfile, NodeProfileRegistry
from app.agent_engine.skill_loader import SkillLoader
from app.agent_engine.tools import ToolRegistry, ToolSpec


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValueInput(StrictModel):
    value: int
    user_facing_message: str = ""


class ValueOutput(StrictModel):
    value: int


class FinalOutput(StrictModel):
    value: int
    user_facing_message: str


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCall:
    encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    return ToolCall(
        call_id=call_id,
        name=name,
        arguments=encoded,
        provider_item={
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": encoded,
        },
    )


def _request(
    run_id: str,
    *,
    budget: AgentBudget,
    tools: tuple[str, ...] = ("work",),
    debug: bool = False,
) -> AgentRequest:
    return AgentRequest(
        node_name="test_node",
        run_id=run_id,
        task="完成测试任务。",
        system_prompt="# 角色与任务\n使用允许的工具完成任务。",
        input_context={"run_id": run_id},
        skill_allowlist=(),
        tool_allowlist=tools,
        completion_model=FinalOutput,
        budget=budget,
        debug=debug,
    )


def _tools(handler: Any, *, names: tuple[str, ...] = ("work",)) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(
            ToolSpec(
                name=name,
                description=f"测试工具 {name}。",
                input_model=ValueInput,
                output_model=ValueOutput,
                allowed_nodes=frozenset({"test_node"}),
                semantic_argument_fields=("value",),
                handler=handler,
            )
        )
    return registry


@pytest.mark.asyncio
async def test_tool_registry_retries_only_explicitly_retryable_errors() -> None:
    class TemporaryFailure(RuntimeError):
        code = "UPSTREAM_BUSY"
        retryable = True

    attempts = 0

    async def flaky(value: ValueInput, _: Any) -> ValueOutput:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TemporaryFailure("temporary")
        return ValueOutput(value=value.value + 1)

    tools = ToolRegistry()
    spec = ToolSpec(
        name="work",
        description="有限重试测试工具。",
        input_model=ValueInput,
        output_model=ValueOutput,
        allowed_nodes=frozenset({"test_node"}),
        max_retries=1,
        handler=flaky,
    )
    tools.register(spec)
    context = RunContext(
        node_name="test_node",
        run_id="retryable",
        cancellation=CancellationToken(),
        objects=MemoryObjectStore(),
    )

    result, actual_attempts = await tools.execute(
        spec,
        {"value": 1},
        context,
        timeout_seconds=2,
    )

    assert isinstance(result, ValueOutput)
    assert result.value == 2
    assert actual_attempts == 2
    assert attempts == 2


@pytest.mark.asyncio
async def test_tool_registry_does_not_retry_permanent_errors() -> None:
    attempts = 0

    async def broken(_: ValueInput, __: Any) -> ValueOutput:
        nonlocal attempts
        attempts += 1
        raise ValueError("permanent")

    tools = ToolRegistry()
    spec = ToolSpec(
        name="work",
        description="永久失败测试工具。",
        input_model=ValueInput,
        output_model=ValueOutput,
        allowed_nodes=frozenset({"test_node"}),
        max_retries=3,
        handler=broken,
    )
    tools.register(spec)
    context = RunContext(
        node_name="test_node",
        run_id="permanent",
        cancellation=CancellationToken(),
        objects=MemoryObjectStore(),
    )

    with pytest.raises(AgentToolError) as exc_info:
        await tools.execute(spec, {"value": 1}, context, timeout_seconds=2)

    assert exc_info.value.code == "AGENT_TOOL_EXECUTION_FAILED"
    assert attempts == 1


def test_node_profile_registry_is_fail_closed_and_rejects_duplicates() -> None:
    registry = NodeProfileRegistry()
    profile = NodeAgentProfile(
        node_name="render_html",
        completion_model=FinalOutput,
        skill_allowlist=("layout",),
        tool_allowlist=("work",),
        budget=AgentBudget(),
    )
    registry.register(profile)

    assert registry.get("render_html") is profile
    with pytest.raises(AgentEngineError) as unknown:
        registry.get("article")
    with pytest.raises(AgentEngineError) as duplicate:
        registry.register(profile)
    assert unknown.value.code == "AGENT_PROFILE_UNKNOWN"
    assert duplicate.value.code == "AGENT_PROFILE_DUPLICATE"


@pytest.mark.asyncio
async def test_interleaved_same_failures_cannot_evade_repeat_budget() -> None:
    class Model:
        calls = 0

        async def agent_turn(self, **_: Any) -> ModelTurn:
            names = ("left", "right", "left")
            name = names[min(self.calls, len(names) - 1)]
            self.calls += 1
            call = _call(f"call_{self.calls}", name, {"value": 1})
            return ModelTurn(tool_calls=[call], response_items=[call.provider_item])

    async def fail(_: ValueInput, __: Any) -> ValueOutput:
        raise ValueError("same deterministic failure")

    result = await AgentRunner(model=Model(), tools=_tools(fail, names=("left", "right"))).run(
        _request(
            "run_interleaved",
            budget=AgentBudget(max_steps=6, max_tool_calls=6, max_same_tool_failures=2),
            tools=("left", "right"),
        )
    )

    assert result.status == "degraded"
    assert result.fallback_reason == "AGENT_REPEATED_TOOL_FAILURE"
    assert result.tool_calls == 3


@pytest.mark.asyncio
async def test_user_message_does_not_change_failure_fingerprint() -> None:
    class Model:
        calls = 0

        async def agent_turn(self, **_: Any) -> ModelTurn:
            self.calls += 1
            call = _call(
                f"call_{self.calls}",
                "work",
                {"value": 1, "user_facing_message": f"第 {self.calls} 次说明"},
            )
            return ModelTurn(tool_calls=[call], response_items=[call.provider_item])

    async def fail(_: ValueInput, __: Any) -> ValueOutput:
        raise ValueError("same deterministic failure")

    result = await AgentRunner(model=Model(), tools=_tools(fail)).run(
        _request(
            "run_message_fingerprint",
            budget=AgentBudget(max_steps=5, max_tool_calls=5, max_same_tool_failures=2),
        )
    )

    assert result.fallback_reason == "AGENT_REPEATED_TOOL_FAILURE"
    assert result.tool_calls == 2


@pytest.mark.asyncio
async def test_duplicate_call_id_executes_handler_only_once() -> None:
    executions = 0

    class Model:
        calls = 0

        async def agent_turn(self, **_: Any) -> ModelTurn:
            self.calls += 1
            if self.calls == 1:
                call = _call("call_same", "work", {"value": 1})
                return ModelTurn(tool_calls=[call, call], response_items=[call.provider_item])
            return ModelTurn(text='{"value":2,"user_facing_message":"完成"}')

    async def work(value: ValueInput, _: Any) -> ValueOutput:
        nonlocal executions
        executions += 1
        return ValueOutput(value=value.value + 1)

    result = await AgentRunner(model=Model(), tools=_tools(work)).run(
        _request("run_dedup", budget=AgentBudget(max_steps=3, max_tool_calls=2))
    )

    assert result.status == "completed"
    assert executions == 1
    assert result.tool_calls == 1


@pytest.mark.asyncio
async def test_context_limit_stops_before_model_call() -> None:
    class Model:
        calls = 0

        async def agent_turn(self, **_: Any) -> ModelTurn:
            self.calls += 1
            return ModelTurn(text='{"value":1,"user_facing_message":"不应调用"}')

    model = Model()
    request = _request("run_context", budget=AgentBudget(max_context_tokens=2_048), tools=())
    request = replace(request, input_context={"large": "中" * 20_000})
    result = await AgentRunner(model=model, tools=ToolRegistry()).run(request)

    assert result.status == "degraded"
    assert result.fallback_reason == "AGENT_CONTEXT_EXCEEDED"
    assert model.calls == 0


@pytest.mark.asyncio
async def test_model_timeout_degrades_without_waiting_for_total_budget() -> None:
    class Model:
        async def agent_turn(self, **_: Any) -> ModelTurn:
            await asyncio.sleep(60)
            return ModelTurn()

    result = await AgentRunner(model=Model(), tools=ToolRegistry()).run(
        _request(
            "run_timeout",
            budget=AgentBudget(call_timeout_seconds=0.01, total_timeout_seconds=1),
            tools=(),
        )
    )
    assert result.status == "degraded"
    assert result.fallback_reason == "AGENT_TIMEOUT"


@pytest.mark.asyncio
async def test_tool_call_budget_counts_each_call_in_a_multi_call_turn() -> None:
    executions = 0

    class Model:
        async def agent_turn(self, **_: Any) -> ModelTurn:
            first = _call("call_1", "work", {"value": 1})
            second = _call("call_2", "work", {"value": 2})
            return ModelTurn(
                tool_calls=[first, second],
                response_items=[first.provider_item, second.provider_item],
            )

    async def work(value: ValueInput, _: Any) -> ValueOutput:
        nonlocal executions
        executions += 1
        return ValueOutput(value=value.value)

    result = await AgentRunner(model=Model(), tools=_tools(work)).run(
        _request("run_tool_budget", budget=AgentBudget(max_steps=2, max_tool_calls=1))
    )
    assert result.status == "degraded"
    assert result.fallback_reason == "AGENT_MAX_TOOL_CALLS"
    assert executions == 1


@pytest.mark.asyncio
async def test_per_tool_budget_degrades_immediately() -> None:
    executions = 0

    class Model:
        async def agent_turn(self, **_: Any) -> ModelTurn:
            call = _call("call_1", "work", {"value": 1})
            return ModelTurn(tool_calls=[call], response_items=[call.provider_item])

    async def work(value: ValueInput, _: Any) -> ValueOutput:
        nonlocal executions
        executions += 1
        return ValueOutput(value=value.value)

    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="work",
            description="没有调用预算的测试工具。",
            input_model=ValueInput,
            output_model=ValueOutput,
            allowed_nodes=frozenset({"test_node"}),
            max_calls=0,
            handler=work,
        )
    )
    result = await AgentRunner(model=Model(), tools=tools).run(
        _request("run_per_tool_budget", budget=AgentBudget(max_steps=3, max_tool_calls=3))
    )

    assert result.status == "degraded"
    assert result.fallback_reason == "AGENT_TOOL_MAX_CALLS"
    assert executions == 0


@pytest.mark.asyncio
async def test_unknown_tool_is_reported_to_model_and_never_executed() -> None:
    class Model:
        calls = 0

        async def agent_turn(self, **kwargs: Any) -> ModelTurn:
            self.calls += 1
            if self.calls == 1:
                call = _call("call_1", "unknown", {"value": 1})
                return ModelTurn(tool_calls=[call], response_items=[call.provider_item])
            output = next(
                item for item in kwargs["input_items"] if item.get("type") == "function_call_output"
            )
            assert "AGENT_TOOL_NOT_ALLOWED" in output["output"]
            return ModelTurn(text='{"value":1,"user_facing_message":"不应完成"}')

    result = await AgentRunner(model=Model(), tools=ToolRegistry()).run(
        _request(
            "run_unknown",
            budget=AgentBudget(max_steps=2, max_tool_calls=2, max_same_tool_failures=2),
            tools=(),
        )
    )
    assert result.status == "degraded"
    assert result.fallback_reason == "AGENT_MAX_STEPS"
    assert result.tool_calls == 1


@pytest.mark.asyncio
async def test_parent_task_cancellation_stops_model_operation() -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    class Model:
        async def agent_turn(self, **_: Any) -> ModelTurn:
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                stopped.set()
            return ModelTurn()

    task = asyncio.create_task(
        AgentRunner(model=Model(), tools=ToolRegistry()).run(
            _request("run_model_cancel", budget=AgentBudget(call_timeout_seconds=120), tools=())
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(stopped.wait(), timeout=1)


@pytest.mark.asyncio
async def test_parent_task_cancellation_stops_tool_handler() -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    class Model:
        async def agent_turn(self, **_: Any) -> ModelTurn:
            call = _call("call_1", "work", {"value": 1})
            return ModelTurn(tool_calls=[call], response_items=[call.provider_item])

    async def work(value: ValueInput, _: Any) -> ValueOutput:
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()
        return ValueOutput(value=value.value)

    task = asyncio.create_task(
        AgentRunner(model=Model(), tools=_tools(work)).run(
            _request("run_tool_cancel", budget=AgentBudget(call_timeout_seconds=120))
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(stopped.wait(), timeout=1)


@pytest.mark.asyncio
async def test_debug_flag_controls_tool_arguments_and_results() -> None:
    async def work(value: ValueInput, _: Any) -> ValueOutput:
        return ValueOutput(value=value.value + 1)

    async def run(debug: bool) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []

        class Model:
            calls = 0

            async def agent_turn(self, **_: Any) -> ModelTurn:
                self.calls += 1
                if self.calls == 1:
                    call = _call("call_1", "work", {"value": 1})
                    return ModelTurn(tool_calls=[call], response_items=[call.provider_item])
                return ModelTurn(text='{"value":2,"user_facing_message":"完成"}')

        request = _request("run_debug", budget=AgentBudget(max_steps=3), debug=debug)
        request = replace(
            request,
            event_callback=lambda kind, payload: events.append((kind, payload)),
        )
        result = await AgentRunner(model=Model(), tools=_tools(work)).run(request)
        assert result.status == "completed"
        return events

    hidden = await run(False)
    visible = await run(True)
    hidden_started = next(data for kind, data in hidden if kind == "tool_started")
    hidden_done = next(data for kind, data in hidden if kind == "tool_completed")
    visible_started = next(data for kind, data in visible if kind == "tool_started")
    visible_done = next(data for kind, data in visible if kind == "tool_completed")
    assert hidden_started["arguments"] is None
    assert hidden_done["result"] is None
    assert visible_started["arguments"] == {"value": 1}
    assert visible_done["result"] == {"value": 2}


@pytest.mark.asyncio
async def test_thirty_concurrent_runs_keep_state_isolated() -> None:
    class Model:
        async def agent_turn(self, **kwargs: Any) -> ModelTurn:
            items = kwargs["input_items"]
            run_id = kwargs["run_id"]
            if not any(item.get("type") == "function_call_output" for item in items):
                value = int(run_id.rsplit("_", 1)[1])
                call = _call(f"call_{value}", "work", {"value": value})
                return ModelTurn(tool_calls=[call], response_items=[call.provider_item])
            value = int(run_id.rsplit("_", 1)[1]) + 1
            return ModelTurn(text=json.dumps({"value": value, "user_facing_message": "完成"}))

    async def work(value: ValueInput, _: Any) -> ValueOutput:
        await asyncio.sleep(0)
        return ValueOutput(value=value.value + 1)

    runner = AgentRunner(model=Model(), tools=_tools(work))
    results = await asyncio.gather(
        *(
            runner.run(_request(f"run_{index}", budget=AgentBudget(max_steps=3)))
            for index in range(30)
        )
    )

    assert [result.output["value"] for result in results if result.output] == list(range(1, 31))
    assert all(result.status == "completed" and result.tool_calls == 1 for result in results)


def test_failed_tool_results_are_not_compacted_away() -> None:
    failed = {
        "type": "function_call_output",
        "call_id": "failed",
        "output": '{"ok":false,"error_code":"BROKEN","message":"must remain"}',
    }
    items: list[dict[str, Any]] = [
        {"role": "system", "content": "rules"},
        {"type": "function_call", "call_id": "failed", "name": "work", "arguments": "{}"},
        failed,
    ]
    for index in range(8):
        items.extend(
            [
                {"type": "function_call", "call_id": f"ok_{index}", "name": "work", "arguments": "{}"},
                {"type": "function_call_output", "call_id": f"ok_{index}", "output": "x" * 500},
            ]
        )
    snapshot = ContextBudget(max_tokens=3_000, reserve_tokens=100, recent_full_rounds=1).preflight(items)

    assert failed in snapshot.input_items
    assert snapshot.input_items[snapshot.input_items.index(failed) - 1]["call_id"] == "failed"


def test_skill_loader_uses_immutable_startup_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill = root / "example"
    reference = skill / "references"
    reference.mkdir(parents=True)
    manifest = """version: 1
skills:
  - id: example
    path: example
    allowed_nodes: [test_node]
    entrypoint: SKILL.md
    reference_allowlist: [references/*.md]
    max_file_bytes: 1024
    max_total_bytes: 2048
"""
    (root / "integration.yaml").write_text(manifest, encoding="utf-8")
    entrypoint = skill / "SKILL.md"
    entrypoint.write_text("startup content", encoding="utf-8")
    (reference / "one.md").write_text("reference content", encoding="utf-8")
    loader = SkillLoader(root)

    entrypoint.write_text("changed after startup", encoding="utf-8")
    entrypoint.unlink()

    loaded = loader.read("example", node_name="test_node", level="L1")
    assert loaded["content"] == "startup content"
    assert loader.metadata("example", node_name="test_node")["asset_sha256"]


@pytest.mark.parametrize(
    ("max_file_bytes", "max_total_bytes", "code"),
    [
        (4, 2048, "AGENT_SKILL_TOO_LARGE"),
        (1024, 8, "AGENT_SKILL_TOTAL_TOO_LARGE"),
    ],
)
def test_skill_loader_rejects_oversized_assets_at_startup(
    tmp_path: Path,
    max_file_bytes: int,
    max_total_bytes: int,
    code: str,
) -> None:
    root = tmp_path / "skills"
    skill = root / "example"
    skill.mkdir(parents=True)
    (root / "integration.yaml").write_text(
        f"""version: 1
skills:
  - id: example
    path: example
    allowed_nodes: [test_node]
    entrypoint: SKILL.md
    max_file_bytes: {max_file_bytes}
    max_total_bytes: {max_total_bytes}
""",
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text("content is deliberately larger", encoding="utf-8")

    with pytest.raises(AgentEngineError) as raised:
        SkillLoader(root)
    assert raised.value.code == code


@pytest.mark.parametrize(
    ("manifest", "code"),
    [
        (
            """version: 1
skills:
  - {id: duplicate, path: one, allowed_nodes: [test_node]}
  - {id: duplicate, path: two, allowed_nodes: [test_node]}
""",
            "AGENT_SKILL_DUPLICATE",
        ),
        (
            """version: 1
skills:
  - {id: escaped, path: ../outside, allowed_nodes: [test_node]}
""",
            "AGENT_SKILL_PATH_FORBIDDEN",
        ),
    ],
)
def test_skill_manifest_fails_closed(tmp_path: Path, manifest: str, code: str) -> None:
    root = tmp_path / "skills"
    (root / "one").mkdir(parents=True)
    (root / "two").mkdir()
    (root / "integration.yaml").write_text(manifest, encoding="utf-8")
    with pytest.raises(AgentEngineError) as raised:
        SkillLoader(root)
    assert raised.value.code == code
