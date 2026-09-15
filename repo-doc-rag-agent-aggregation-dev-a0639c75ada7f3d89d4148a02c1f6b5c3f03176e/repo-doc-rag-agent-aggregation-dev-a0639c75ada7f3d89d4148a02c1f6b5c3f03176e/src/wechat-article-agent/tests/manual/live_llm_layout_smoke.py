"""Run one real no-document workflow and approve every HITL card.

This script is intentionally outside the application package. It validates the
public Responses-like contract and writes a local JSON report without changing
business state beyond the newly generated, disposable test session.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "http://127.0.0.1:8240"
REPORT = Path("/tmp/wechat-article-llm-layout-smoke.json")
PROJECT = Path(__file__).parents[2]
ARTIFACT_DIR = PROJECT / "docs" / "smoke-artifacts"


async def stream_turn(client: httpx.AsyncClient, payload: dict[str, Any]) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    event_name = "message"
    data_lines: list[str] = []
    async with client.stream("POST", f"{BASE_URL}/v1/responses", json=payload) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line and data_lines:
                raw = "\n".join(data_lines)
                data_lines = []
                if raw != "[DONE]":
                    events.append({"event": event_name, "data": json.loads(raw)})
                event_name = "message"
    return {
        "events": events,
        "response_id": next(
            item["data"]["response"]["id"] for item in events if item["event"] == "response.created"
        ),
        "interrupt": next((item["data"] for item in events if item["event"] == "agent.interrupt"), None),
        "text": "".join(
            item["data"].get("delta", "") for item in events if item["event"] == "response.output_text.delta"
        ),
        "final": next(
            (
                item["data"]
                for item in reversed(events)
                if item["event"] in {"response.completed", "response.failed"}
            ),
            None,
        ),
    }


def hitl_payload(interrupt_event: dict[str, Any]) -> tuple[dict[str, Any], str]:
    interrupt = interrupt_event["interrupt"]
    stage = str(interrupt.get("stage") or "")
    form_type = str(interrupt.get("form", {}).get("form_type") or "")
    hitl: dict[str, Any] = {
        "interrupt_id": interrupt["id"],
        "decision": "approve",
        "feedback": "",
    }
    user_text = f"接受当前{stage}结果"
    if form_type == "agent_clarification":
        hitl.update({"decision": "revise", "selection": {"option_id": "A"}})
        user_text = f"选择{stage}的 A 方案"
    elif "conflict" in stage:
        hitl.update(
            {
                "decision": "revise",
                "selection": {"option_id": "automatic_authority"},
            }
        )
        user_text = "按照来源权威性自动处理素材冲突"
    return hitl, user_text


async def main() -> None:
    session_id = f"layout-smoke-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    history: list[dict[str, str]] = [
        {
            "role": "user",
            "content": (
                "请基于通识生成一篇面向中小企业管理者的微信公众号文章，主题是如何建立实用的"
                "数据安全管理制度，语气专业清晰，篇幅约 900 字。本次回归烟测不需要配图。"
            ),
        }
    ]
    turns: list[dict[str, Any]] = []
    request: dict[str, Any] = {
        "model": "wechat-article-agent",
        "stream": True,
        "input": history,
        "context": {
            "session_id": session_id,
            "user_id": "wechat-article-agent-dev-user",
            "kb_id": "wechat-article-agent-dev-kb",
            "doc_ids": [],
            "temp_doc_ids": [],
            "debug": True,
        },
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10)) as client:
        for _ in range(8):
            turn = await stream_turn(client, request)
            turns.append(turn)
            if turn["text"]:
                history.append({"role": "assistant", "content": turn["text"]})
            final = turn.get("final") or {}
            workflow_status = final.get("response", {}).get("metadata", {}).get("workflow_status")
            if workflow_status == "completed":
                break
            if not turn["interrupt"]:
                raise RuntimeError(f"workflow stopped without an interrupt: {workflow_status}")
            hitl, user_text = hitl_payload(turn["interrupt"])
            history.append({"role": "user", "content": user_text})
            request = {
                "model": "wechat-article-agent",
                "stream": True,
                "input": history,
                "previous_response_id": turn["response_id"],
                "context": {
                    "session_id": session_id,
                    "user_id": "wechat-article-agent-dev-user",
                    "kb_id": "wechat-article-agent-dev-kb",
                    "doc_ids": [],
                    "temp_doc_ids": [],
                    "debug": True,
                    "hitl": hitl,
                },
            }
        else:
            raise RuntimeError("workflow did not complete within eight turns")

    last = turns[-1]
    debug = ((last.get("final") or {}).get("debug") or {}).get("trace") or {}
    activities = [
        item["data"]["activity"]
        for turn in turns
        for item in turn["events"]
        if item["event"] == "agent.activity"
    ]
    artifacts = [
        item["data"]["artifact"]
        for turn in turns
        for item in turn["events"]
        if item["event"] == "agent.artifact"
    ]
    report = {
        "session_id": session_id,
        "turn_count": len(turns),
        "workflow_status": last["final"]["response"]["metadata"]["workflow_status"],
        "public_text": [turn["text"] for turn in turns],
        "output_text_delta_count": sum(
            1 for turn in turns for item in turn["events"] if item["event"] == "response.output_text.delta"
        ),
        "activity_names": [f"{item.get('name')}:{item.get('status')}" for item in activities],
        "artifact_stages": [item["stage"] for item in artifacts],
        "final_html_chars": max(
            (len(item["content"]) for item in artifacts if item["stage"] == "final_html"),
            default=0,
        ),
        "llm_calls": debug.get("llm_calls", []),
        "tool_calls": debug.get("tool_calls", []),
        "raw_turns": turns,
    }
    completed_activity_names = {
        str(item.get("name")) for item in activities if item.get("status") == "completed"
    }
    required_activity_names = {
        "wechat_layout.skill_driven",
        "agent_engine.load_rendering_skill_asset",
        "agent_engine.analyze_markdown_layout",
        "agent_engine.get_skill_theme_catalog",
        "agent_engine.get_skill_component_catalog",
        "agent_engine.validate_skill_layout_plan",
        "agent_engine.assemble_skill_html",
        "agent_engine.validate_skill_html",
    }
    missing_activities = sorted(required_activity_names - completed_activity_names)
    engine_llm_calls = [
        item for item in debug.get("llm_calls", []) if item.get("phase") == "agent_render_html"
    ]
    engine_tool_calls = [
        item
        for item in debug.get("tool_calls", [])
        if str(item.get("name") or "").startswith("agent_engine.")
    ]
    report["engine_llm_call_count"] = len(engine_llm_calls)
    report["engine_debug_tool_call_count"] = len(engine_tool_calls)
    report["missing_required_activities"] = missing_activities
    final_html = next(
        (
            str(item["content"])
            for item in reversed(artifacts)
            if item["stage"] == "final_html" and item.get("content")
        ),
        "",
    )
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    if final_html:
        html_path = ARTIFACT_DIR / f"skill-rendering-workflow-{stamp}.html"
        html_path.write_text(final_html, encoding="utf-8")
        report["html_path"] = str(html_path.relative_to(PROJECT))
    summary_path = ARTIFACT_DIR / f"skill-rendering-workflow-{stamp}.json"
    report["summary_path"] = str(summary_path.relative_to(PROJECT))
    report_json = json.dumps(report, ensure_ascii=False, indent=2)
    await asyncio.to_thread(REPORT.write_text, report_json, encoding="utf-8")
    summary = {key: value for key, value in report.items() if key != "raw_turns"}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report={REPORT}")
    if report["workflow_status"] != "completed":
        raise RuntimeError("workflow did not complete")
    if not final_html:
        raise RuntimeError("workflow did not return final_html")
    if missing_activities:
        raise RuntimeError(f"skill_driven activities are incomplete: {missing_activities}")
    if report["output_text_delta_count"] < 10:
        raise RuntimeError("public explanation text was not streamed as incremental deltas")
    if not engine_llm_calls:
        raise RuntimeError("debug trace is missing agent_render_html LLM calls")
    if not engine_tool_calls:
        raise RuntimeError("debug trace is missing Agent Engine tool calls")


if __name__ == "__main__":
    asyncio.run(main())
