from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from langgraph_sdk.errors import ConflictError, NotFoundError

from app.runtime.client import GraphRuntimeClient

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("WECHAT_RUN_LIVE_CONTRACTS") != "1",
        reason="set WECHAT_RUN_LIVE_CONTRACTS=1 with the local Agent Server running",
    ),
]


async def _drain(parts: AsyncIterator[Any]) -> None:
    async for _ in parts:
        pass


@pytest.mark.asyncio
async def test_agent_server_interrupt_resume_cancel_reject_and_delete() -> None:
    runtime = GraphRuntimeClient(
        url=os.getenv("WECHAT_AGENT_SERVER_URL", "http://127.0.0.1:8242"),
        assistant_id="contract_probe",
        thread_ttl_minutes=5,
    )
    created_threads: list[str] = []
    try:
        approved_thread = str(uuid4())
        created_threads.append(approved_thread)
        await runtime.ensure_thread(approved_thread)
        first_run, parts = await runtime.start_stream(
            approved_thread,
            input={"response_id": "resp_contract_1", "interrupt_id": "int_contract_1"},
        )
        await _drain(parts)
        assert (await runtime.run(approved_thread, first_run))["status"] == "success"
        waiting = await runtime.state(approved_thread)
        assert waiting["values"]["status"] == "waiting_for_input"
        second_run, parts = await runtime.start_stream(
            approved_thread,
            resume={"decision": "approve", "response_id": "resp_contract_2"},
        )
        await _drain(parts)
        assert (await runtime.run(approved_thread, second_run))["status"] == "success"
        completed = await runtime.state(approved_thread)
        assert completed["values"]["status"] == "completed"
        assert completed["values"]["response_id"] == "resp_contract_2"

        pending_cancel_thread = str(uuid4())
        created_threads.append(pending_cancel_thread)
        await runtime.ensure_thread(pending_cancel_thread)
        _, parts = await runtime.start_stream(
            pending_cancel_thread,
            input={"response_id": "resp_cancel", "interrupt_id": "int_cancel"},
        )
        await _drain(parts)
        cancel_control = await runtime.create_run(
            pending_cancel_thread,
            resume={"type": "cancel", "response_id": "resp_cancel"},
        )
        await runtime.join(pending_cancel_thread, str(cancel_control["run_id"]))
        cancelled_state = await runtime.state(pending_cancel_thread)
        assert cancelled_state["values"]["status"] == "cancelled"

        running_thread = str(uuid4())
        created_threads.append(running_thread)
        await runtime.ensure_thread(running_thread)
        running_id, parts = await runtime.start_stream(
            running_thread,
            input={
                "response_id": "resp_running",
                "interrupt_id": "int_running",
                "delay_seconds": 30,
            },
        )
        with pytest.raises(ConflictError):
            await runtime.create_run(
                running_thread,
                input={"response_id": "resp_conflict", "interrupt_id": "int_conflict"},
            )
        await runtime.cancel_run(running_thread, running_id, wait=True)
        closer = getattr(parts, "aclose", None)
        if closer is not None:
            await closer()
        assert (await runtime.run(running_thread, running_id))["status"] == "interrupted"

        await runtime.refresh_thread_ttl(approved_thread)
        await runtime.delete_thread(approved_thread)
        created_threads.remove(approved_thread)
        with pytest.raises(NotFoundError):
            await runtime.thread(approved_thread)
    finally:
        for thread_id in created_threads:
            try:
                await runtime.delete_thread(thread_id)
            except NotFoundError:
                pass
        await runtime.close()
