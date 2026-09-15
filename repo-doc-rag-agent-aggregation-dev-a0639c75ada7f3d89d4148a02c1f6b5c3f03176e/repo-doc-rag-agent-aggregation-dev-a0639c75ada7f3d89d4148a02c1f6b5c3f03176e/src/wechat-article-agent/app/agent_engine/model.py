from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel

from app.agent_engine.contracts import EngineEventCallback, ModelTurn


class GatewayAgentModel:
    """Small adapter that keeps AgentRunner independent from the application gateway."""

    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway

    async def agent_turn(
        self,
        *,
        run_id: str,
        call_id: str,
        phase: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        output_model: type[BaseModel],
        max_tool_calls: int,
        timeout_seconds: float,
        event_callback: EngineEventCallback | None = None,
    ) -> ModelTurn:
        return cast(
            ModelTurn,
            await self.gateway.agent_turn(
                run_id=run_id,
                call_id=call_id,
                phase=phase,
                input_items=input_items,
                tools=tools,
                output_model=output_model,
                max_tool_calls=max_tool_calls,
                timeout_seconds=timeout_seconds,
                event_callback=event_callback,
            ),
        )
