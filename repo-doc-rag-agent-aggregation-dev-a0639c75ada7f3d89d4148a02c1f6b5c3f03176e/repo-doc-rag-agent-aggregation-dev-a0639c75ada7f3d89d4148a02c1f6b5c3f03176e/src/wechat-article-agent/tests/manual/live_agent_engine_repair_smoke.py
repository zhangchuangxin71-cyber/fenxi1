"""Exercise one real Ark tool loop with a forced first-pass validator rejection."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PROJECT = Path(__file__).parents[2]
sys.path.insert(0, str(PROJECT))

from app.agent_engine import AgentBudget, AgentRequest, AgentRunner  # noqa: E402
from app.agent_engine.errors import AgentEngineError  # noqa: E402
from app.agent_engine.model import GatewayAgentModel  # noqa: E402
from app.agent_engine.tools import ToolRegistry, ToolSpec  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.llm.gateway import LLMGateway  # noqa: E402

OUTPUT = PROJECT / "docs" / "smoke-artifacts"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EchoInput(StrictModel):
    value: str = Field(description="原样回显的短文本。")
    user_facing_message: str = Field("", description="面向用户的简短操作说明。")


class EchoOutput(StrictModel):
    value: str


class Completion(StrictModel):
    value: str = Field(description="必须等于工具返回的 value。")
    user_facing_message: str = Field(description="面向用户的简短完成说明。")


async def main() -> None:
    settings = get_settings()
    gateway = LLMGateway(settings)
    tools = ToolRegistry()
    events: list[dict[str, Any]] = []
    validation_attempts = 0

    async def echo(value: EchoInput, context: Any) -> EchoOutput:
        context.metadata["expected_value"] = value.value
        return EchoOutput(value=value.value)

    async def observe(kind: str, payload: dict[str, Any]) -> None:
        events.append({"kind": kind, "payload": payload})

    def validate(output: BaseModel, context: Any) -> None:
        nonlocal validation_attempts
        validation_attempts += 1
        if validation_attempts == 1:
            raise AgentEngineError(
                "SMOKE_REVIEW_RETRY",
                "测试 validator 主动拒绝第一次完成结果，请按原工具证据重新提交。",
            )
        if output.value != context.metadata.get("expected_value"):
            raise AgentEngineError("SMOKE_VALUE_MISMATCH", "完成值与工具结果不一致。")

    tools.register(
        ToolSpec(
            name="echo_plan",
            description="无副作用地回显一段短文本，验证 Ark function calling。",
            input_model=EchoInput,
            output_model=EchoOutput,
            allowed_nodes=frozenset({"contract_probe"}),
            side_effect="memory_only",
            max_calls=2,
            handler=echo,
        )
    )
    request = AgentRequest(
        node_name="contract_probe",
        run_id=f"live_repair_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
        task="先调用 echo_plan 回显 fixed-value，再依据工具结果提交完成 JSON。",
        system_prompt=(
            "# 角色与任务\n完成 function-call 契约验证。\n\n"
            "# 工作原则\n必须先调用一次 echo_plan，再返回 strict completion。\n\n"
            "# 禁止事项\n不得猜测工具结果，不得输出内部推理。"
        ),
        input_context={"value": "fixed-value"},
        skill_allowlist=(),
        tool_allowlist=("echo_plan",),
        completion_model=Completion,
        budget=AgentBudget(
            max_steps=5,
            max_tool_calls=2,
            max_context_tokens=settings.agent_engine_max_context_tokens,
            context_reserve_tokens=settings.agent_engine_context_reserve_tokens,
            call_timeout_seconds=settings.agent_engine_call_timeout_seconds,
            total_timeout_seconds=settings.agent_engine_total_timeout_seconds,
        ),
        event_callback=observe,
        completion_validator=validate,
    )
    try:
        result = await AgentRunner(model=GatewayAgentModel(gateway), tools=tools).run(request)
    finally:
        await gateway.close()

    report = {
        "status": result.status,
        "steps": result.steps,
        "tool_calls": result.tool_calls,
        "fallback_reason": result.fallback_reason,
        "validation_attempts": validation_attempts,
        "output": result.output,
        "event_kinds": [item["kind"] for item in events],
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = OUTPUT / f"agent-engine-repair-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(path.relative_to(PROJECT))
    print(json.dumps(report, ensure_ascii=False, indent=2))

    assert result.status == "completed"
    assert validation_attempts == 2
    assert result.output is not None and result.output["value"] == "fixed-value"
    assert "final_output_invalid" in report["event_kinds"]


if __name__ == "__main__":
    asyncio.run(main())
