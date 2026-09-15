from __future__ import annotations

import asyncio
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.events.internal import activity, emit


class ProbeState(TypedDict, total=False):
    value: str
    response_id: str
    interrupt_id: str
    decision: str
    status: str
    delay_seconds: float


async def work(state: ProbeState) -> ProbeState:
    activity(
        activity_id="probe_work",
        kind="node",
        name="probe_work",
        label="协议探针",
        status="running",
        node="contract_probe",
    )
    delay = float(state.get("delay_seconds", 0))
    if delay:
        await asyncio.sleep(delay)
    emit("public_text", {"text": "协议探针已生成待审批结果。"})
    activity(
        activity_id="probe_work",
        kind="node",
        name="probe_work",
        label="协议探针",
        status="completed",
        node="contract_probe",
    )
    return {
        "value": "candidate",
        "interrupt_id": state.get("interrupt_id", "probe_interrupt"),
        "status": "waiting_for_input",
    }


def review(state: ProbeState) -> Command[str]:
    payload = {
        "interrupt_id": state["interrupt_id"],
        "response_id": state["response_id"],
        "stage": "probe_review",
        "value": state["value"],
    }
    resume = interrupt(payload)
    if not isinstance(resume, dict):
        raise ValueError("probe resume must be an object")
    return Command(
        update={
            "response_id": str(resume.get("response_id") or state["response_id"]),
            "decision": str(resume.get("decision") or resume.get("type") or ""),
            "status": "cancelled" if resume.get("type") == "cancel" else "completed",
        },
        goto="finish",
    )


def finish(state: ProbeState) -> ProbeState:
    emit("probe.finished", {"decision": state.get("decision"), "status": state["status"]})
    return {"status": state["status"]}


builder = StateGraph(ProbeState)
builder.add_node("work", work)
builder.add_node("review", review)
builder.add_node("finish", finish)
builder.add_edge(START, "work")
builder.add_edge("work", "review")
builder.add_edge("finish", END)
graph = builder.compile(name="contract_probe")
