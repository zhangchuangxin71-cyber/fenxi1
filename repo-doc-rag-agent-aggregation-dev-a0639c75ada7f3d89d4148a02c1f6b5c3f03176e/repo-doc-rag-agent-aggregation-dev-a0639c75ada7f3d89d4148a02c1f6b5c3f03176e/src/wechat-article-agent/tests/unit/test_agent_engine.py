from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.agent_engine import AgentBudget, AgentRequest, AgentRunner, CancellationToken, ModelTurn, ToolCall
from app.agent_engine.context import ContextBudget, MemoryObjectStore
from app.agent_engine.errors import AgentEngineError
from app.agent_engine.skill_loader import SkillLoader
from app.agent_engine.tools import ToolRegistry, ToolSpec


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IncrementInput(StrictModel):
    value: int


class AgentOutput(StrictModel):
    value: int
    user_facing_message: str


def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCall:
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


def request(
    budget: AgentBudget,
    *,
    cancellation: CancellationToken | None = None,
    completion_validator: Any = None,
) -> AgentRequest:
    return AgentRequest(
        node_name="render_html",
        run_id="run_test",
        task="完成测试排版任务。",
        system_prompt="# 角色与任务\n使用允许工具完成任务。",
        input_context={"topic": "测试"},
        skill_allowlist=(),
        tool_allowlist=("increment",),
        completion_model=AgentOutput,
        budget=budget,
        cancellation=cancellation or CancellationToken(),
        completion_validator=completion_validator,
    )


@pytest.mark.asyncio
async def test_runner_performs_real_model_tool_result_model_loop() -> None:
    observed: list[list[dict[str, Any]]] = []

    class Model:
        calls = 0

        async def agent_turn(self, **kwargs: Any) -> ModelTurn:
            self.calls += 1
            observed.append(kwargs["input_items"])
            if self.calls == 1:
                call = tool_call("call_1", "increment", {"value": 1})
                return ModelTurn(tool_calls=[call], response_items=[call.provider_item])
            return ModelTurn(text='{"value":2,"user_facing_message":"处理完成。"}')

    async def increment(value: IncrementInput, context: Any) -> IncrementInput:
        del context
        return IncrementInput(value=value.value + 1)

    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="increment",
            description="递增输入数值。",
            input_model=IncrementInput,
            output_model=IncrementInput,
            allowed_nodes=frozenset({"render_html"}),
            handler=increment,
        )
    )
    result = await AgentRunner(model=Model(), tools=tools).run(
        request(AgentBudget(max_steps=4, max_tool_calls=2))
    )

    assert result.status == "completed"
    assert result.output == {"value": 2, "user_facing_message": "处理完成。"}
    assert result.steps == 2
    assert result.tool_calls == 1
    assert any(item.get("type") == "function_call_output" for item in observed[1])


@pytest.mark.asyncio
async def test_runner_can_validate_final_candidate_and_reask_within_step_budget() -> None:
    class Model:
        calls = 0

        async def agent_turn(self, **kwargs: Any) -> ModelTurn:
            del kwargs
            self.calls += 1
            if self.calls == 1:
                return ModelTurn(text='{"value":1,"user_facing_message":"候选"}')
            return ModelTurn(text='{"value":2,"user_facing_message":"已修正"}')

    seen: list[int] = []

    async def validate(output: BaseModel, context: Any) -> None:
        del context
        seen.append(output.value)
        if output.value != 2:
            from app.agent_engine.errors import AgentEngineError

            raise AgentEngineError("CANDIDATE_INVALID", "候选值必须为 2。")

    result = await AgentRunner(model=Model(), tools=ToolRegistry()).run(
        request(AgentBudget(max_steps=3, max_tool_calls=0), completion_validator=validate)
    )
    assert result.status == "completed"
    assert seen == [1, 2]


@pytest.mark.asyncio
async def test_same_tool_failure_degrades_after_configured_threshold() -> None:
    class Model:
        calls = 0

        async def agent_turn(self, **kwargs: Any) -> ModelTurn:
            del kwargs
            self.calls += 1
            call = tool_call(f"call_{self.calls}", "increment", {"value": 1})
            return ModelTurn(tool_calls=[call], response_items=[call.provider_item])

    async def fail(value: IncrementInput, context: Any) -> IncrementInput:
        del value, context
        raise ValueError("synthetic")

    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="increment",
            description="故意失败。",
            input_model=IncrementInput,
            output_model=IncrementInput,
            allowed_nodes=frozenset({"render_html"}),
            handler=fail,
        )
    )
    result = await AgentRunner(model=Model(), tools=tools).run(
        request(AgentBudget(max_steps=5, max_tool_calls=5, max_same_tool_failures=2))
    )
    assert result.status == "degraded"
    assert result.fallback_reason == "AGENT_REPEATED_TOOL_FAILURE"
    assert result.tool_calls == 2


@pytest.mark.asyncio
async def test_failed_tool_cannot_be_hidden_by_an_immediate_final_json() -> None:
    class Model:
        calls = 0

        async def agent_turn(self, **kwargs: Any) -> ModelTurn:
            del kwargs
            self.calls += 1
            if self.calls == 1:
                call = tool_call("call_1", "increment", {"value": 1})
                return ModelTurn(tool_calls=[call], response_items=[call.provider_item])
            return ModelTurn(text='{"value":2,"user_facing_message":"不应直接提交"}')

    async def fail(value: IncrementInput, context: Any) -> IncrementInput:
        del value, context
        raise ValueError("synthetic")

    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="increment",
            description="故意失败。",
            input_model=IncrementInput,
            output_model=IncrementInput,
            allowed_nodes=frozenset({"render_html"}),
            handler=fail,
        )
    )
    result = await AgentRunner(model=Model(), tools=tools).run(
        request(AgentBudget(max_steps=2, max_tool_calls=2, max_same_tool_failures=5))
    )
    assert result.status == "degraded"
    assert result.fallback_reason == "AGENT_MAX_STEPS"


def test_context_compaction_preserves_complete_recent_function_pairs() -> None:
    items: list[dict[str, Any]] = [{"role": "system", "content": "rules"}]
    for index in range(20):
        items.extend(
            [
                {"type": "function_call", "call_id": f"call_{index}", "name": "tool", "arguments": "{}"},
                {"type": "function_call_output", "call_id": f"call_{index}", "output": "x" * 500},
            ]
        )
    snapshot = ContextBudget(
        max_tokens=3200,
        reserve_tokens=200,
        recent_full_rounds=3,
        max_compactions=2,
    ).preflight(items)
    recent = snapshot.input_items[-6:]
    assert snapshot.compressed is True
    assert snapshot.removed_items > 0
    assert any("早期工具轮次摘要" in str(item.get("content")) for item in snapshot.input_items)
    assert [item.get("call_id") for item in recent] == [
        "call_17",
        "call_17",
        "call_18",
        "call_18",
        "call_19",
        "call_19",
    ]


def test_context_compaction_preserves_user_corrections_in_chronological_order() -> None:
    correction = {
        "role": "user",
        "content": "校验错误：图片位置必须原样保留。",
    }
    items: list[dict[str, Any]] = [{"role": "system", "content": "rules"}]
    for index in range(10):
        items.extend(
            [
                {"type": "function_call", "call_id": f"call_{index}", "name": "tool", "arguments": "{}"},
                {"type": "function_call_output", "call_id": f"call_{index}", "output": "x" * 400},
            ]
        )
        if index == 4:
            items.append(correction)

    snapshot = ContextBudget(
        max_tokens=2100,
        reserve_tokens=100,
        recent_full_rounds=2,
        max_compactions=2,
    ).preflight(items)

    correction_index = snapshot.input_items.index(correction)
    call_8_index = next(
        index for index, item in enumerate(snapshot.input_items) if item.get("call_id") == "call_8"
    )
    assert correction_index < call_8_index
    assert snapshot.input_items[correction_index] == correction


@pytest.mark.asyncio
async def test_cancellation_propagates_and_clears_run_objects() -> None:
    cancellation = CancellationToken()
    exposed: list[MemoryObjectStore] = []
    started = asyncio.Event()

    class Model:
        async def agent_turn(self, **kwargs: Any) -> ModelTurn:
            del kwargs
            call = tool_call("call_1", "increment", {"value": 1})
            return ModelTurn(tool_calls=[call], response_items=[call.provider_item])

    async def slow(value: IncrementInput, context: Any) -> IncrementInput:
        context.objects.put({"candidate": value.value})
        exposed.append(context.objects)
        started.set()
        await asyncio.sleep(60)
        return value

    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="increment",
            description="慢工具。",
            input_model=IncrementInput,
            output_model=IncrementInput,
            allowed_nodes=frozenset({"render_html"}),
            handler=slow,
        )
    )
    task = asyncio.create_task(
        AgentRunner(model=Model(), tools=tools).run(
            request(AgentBudget(max_steps=4, max_tool_calls=2), cancellation=cancellation)
        )
    )
    await started.wait()
    cancellation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(exposed[0]) == 0


def test_skill_loader_enforces_node_and_reference_allowlists() -> None:
    root = Path(__file__).parents[2] / "app" / "skills" / "rendering"
    loader = SkillLoader(root)
    metadata = loader.metadata("gzh_design", node_name="render_html")
    workflow = loader.read("gzh_design", node_name="render_html", level="L1")
    reference = loader.read(
        "gzh_design",
        node_name="render_html",
        level="L2",
        reference="references/theme-index.md",
    )
    assert metadata["source"]["commit"] == "ba1f4175519b481cb3566616c9e5178705067904"
    assert "content" not in metadata
    assert "内存装配器" in workflow["content"]
    assert "moyu-green" in reference["content"]
    with pytest.raises(AgentEngineError) as forbidden_node:
        loader.metadata("gzh_design", node_name="generate_article")
    with pytest.raises(AgentEngineError) as forbidden_reference:
        loader.read(
            "gzh_design",
            node_name="render_html",
            level="L2",
            reference="../integration.yaml",
        )
    assert getattr(forbidden_node.value, "code", None) == "AGENT_SKILL_NODE_FORBIDDEN"
    assert getattr(forbidden_reference.value, "code", None) == "AGENT_SKILL_REFERENCE_FORBIDDEN"
