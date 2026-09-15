"""Run the real Ark-backed GZH/Xiaowan rendering Agent against the fixed fixture."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).parents[2]
sys.path.insert(0, str(PROJECT))

from app.agent_engine import AgentBudget  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.llm.gateway import LLMGateway  # noqa: E402
from app.rendering.skill_driven.adapter import run_layout_agent  # noqa: E402

INPUT = PROJECT / "tests" / "rendering_data" / "input"
OUTPUT = PROJECT / "docs" / "smoke-artifacts"


async def main() -> None:
    settings = get_settings()
    gateway = LLMGateway(settings)
    markdown = (INPUT / "article.md").read_text(encoding="utf-8")
    images = json.loads((INPUT / "images.json").read_text(encoding="utf-8"))
    task_spec = json.loads((INPUT / "task_spec.json").read_text(encoding="utf-8"))
    events: list[dict[str, Any]] = []

    async def observe(kind: str, payload: dict[str, Any]) -> None:
        events.append({"kind": kind, "payload": payload})

    try:
        result, rendered = await run_layout_agent(
            gateway=gateway,
            run_id=f"live_layout_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
            markdown=markdown,
            images=images,
            task_spec=task_spec,
            default_theme=settings.html_layout_default_theme,
            budget=AgentBudget(
                max_steps=settings.agent_engine_max_steps,
                max_tool_calls=settings.agent_engine_max_tool_calls,
                max_context_tokens=settings.agent_engine_max_context_tokens,
                context_reserve_tokens=settings.agent_engine_context_reserve_tokens,
                recent_full_rounds=settings.agent_engine_recent_full_rounds,
                max_compactions=settings.agent_engine_max_compactions,
                call_timeout_seconds=settings.agent_engine_call_timeout_seconds,
                total_timeout_seconds=settings.agent_engine_total_timeout_seconds,
                max_same_tool_failures=settings.agent_engine_max_same_tool_failures,
                max_tool_result_chars=settings.agent_engine_max_tool_result_chars,
            ),
            event_callback=observe,
            debug=True,
        )
    finally:
        await gateway.close()

    OUTPUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    report_path = OUTPUT / f"skill-rendering-live-{stamp}.json"
    report = {
        "status": result.status,
        "steps": result.steps,
        "tool_calls": result.tool_calls,
        "compressed": result.compressed,
        "fallback_reason": result.fallback_reason,
        "theme_id": rendered.theme_id if rendered else None,
        "html_chars": len(rendered.final_html) if rendered else 0,
        "events": events,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if rendered is not None:
        html_path = OUTPUT / f"skill-rendering-live-{stamp}.html"
        html_path.write_text(rendered.final_html, encoding="utf-8")
        report["html_path"] = str(html_path.relative_to(PROJECT))
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if result.status != "completed" or rendered is None:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
