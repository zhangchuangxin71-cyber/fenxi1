"""Replay the latest real Skill rendering workflow through the development panel."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

PROJECT = Path(__file__).parents[2]
REPORT = Path("/tmp/wechat-article-llm-layout-smoke.json")
OUTPUT = PROJECT / "docs" / "smoke-artifacts"
PANEL_URL = "http://127.0.0.1:8245"


def replay(page: Page, turns: list[dict[str, Any]]) -> None:
    page.goto(PANEL_URL, wait_until="networkidle")
    page.wait_for_function("typeof createTurn === 'function' && typeof applyEvent === 'function'")
    for index, turn in enumerate(turns, 1):
        page.evaluate(
            """({index}) => createTurn({input: [{role: "user", content: `烟测审批轮次 ${index}`} ]})""",
            {"index": index},
        )
        for item in turn["events"]:
            page.evaluate(
                """({name, data}) => applyEvent(name, data, JSON.stringify(data))""",
                {"name": item["event"], "data": item["data"]},
            )
    page.evaluate("flushRender()")
    page.wait_for_timeout(300)


def verify_page(page: Page) -> dict[str, Any]:
    callable_cards = page.locator(".callable-activity")
    final_card = page.locator(".artifact-final_html").last
    final_card.locator(":scope > summary").click()
    page.wait_for_timeout(100)

    iframe = final_card.locator("iframe.html-frame")
    iframe.wait_for(state="attached")
    frame = iframe.content_frame
    frame.locator("body").wait_for(state="attached")
    html_body_text = frame.locator("body").inner_text().strip()

    first_callable = callable_cards.first
    first_callable.locator(":scope > summary").click()
    page.wait_for_timeout(100)
    was_open = first_callable.evaluate("element => element.open")
    page.evaluate(
        """() => {
          const turn = state.currentTurn;
          const activity = [...turn.activities.values()].find(
            item => item.kind === "tool" || item.kind === "skill"
          );
          applyEvent("agent.activity", {
            run_id: turn.runId,
            response_id: turn.responseId,
            activity,
          }, JSON.stringify({activity}));
          flushRender();
        }"""
    )
    first_key = first_callable.get_attribute("data-call-key")
    if first_key:
        current_callable = page.locator(f'.callable-activity[data-call-key="{first_key}"]')
    else:
        current_callable = page.locator(".callable-activity").first
    remained_open = current_callable.evaluate("element => element.open")
    final_remained_open = page.locator(".artifact-final_html").last.evaluate("element => element.open")

    page.locator('.trace-tab[data-tab="llm"]').click()
    llm_calls = page.locator("#llm-view .llm-call").count()
    llm_titles = page.locator("#llm-view .llm-call > summary").all_inner_texts()
    page.locator('.trace-tab[data-tab="tool"]').click()
    tool_calls = page.locator("#tool-view .tool-call").count()
    tool_titles = page.locator("#tool-view .tool-call > summary").all_inner_texts()

    metrics = page.evaluate(
        """() => ({
          bodyScrollWidth: document.documentElement.scrollWidth,
          bodyClientWidth: document.documentElement.clientWidth,
          conversationTurns: document.querySelectorAll(".response-turn").length,
          textBubbles: document.querySelectorAll(".message-row.assistant .message-bubble").length,
          finalArtifacts: document.querySelectorAll(".artifact-final_html").length,
          callableCards: document.querySelectorAll(".callable-activity").length,
          skillCards: document.querySelectorAll(".callable-activity.skill").length,
          toolCards: document.querySelectorAll(".callable-activity.tool").length,
        })"""
    )
    metrics.update(
        {
            "html_body_chars": len(html_body_text),
            "llm_trace_cards": llm_calls,
            "tool_trace_cards": tool_calls,
            "llm_trace_has_agent": any("agent_render_html" in title for title in llm_titles),
            "tool_trace_has_layout_agent": any(
                name in title
                for title in tool_titles
                for name in (
                    "load_rendering_skill_asset",
                    "get_skill_theme_catalog",
                    "assemble_skill_html",
                    "validate_skill_html",
                )
            ),
            "callable_was_open": was_open,
            "callable_remained_open": remained_open,
            "final_artifact_remained_open": final_remained_open,
        }
    )
    return metrics


def assert_metrics(metrics: dict[str, Any]) -> None:
    assert metrics["conversationTurns"] == 5
    assert metrics["textBubbles"] >= 5
    assert metrics["finalArtifacts"] >= 1
    assert metrics["callableCards"] >= 8
    assert metrics["skillCards"] >= 2
    assert metrics["toolCards"] >= 4
    assert metrics["html_body_chars"] > 500
    assert metrics["llm_trace_cards"] >= 1
    assert metrics["tool_trace_cards"] >= 1
    assert metrics["llm_trace_has_agent"] is True
    assert metrics["tool_trace_has_layout_agent"] is True
    assert metrics["callable_was_open"] is True
    assert metrics["callable_remained_open"] is True
    assert metrics["final_artifact_remained_open"] is True
    assert metrics["bodyScrollWidth"] <= metrics["bodyClientWidth"] + 1


def main() -> None:
    raw = json.loads(REPORT.read_text(encoding="utf-8"))
    turns = raw["raw_turns"]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    result: dict[str, Any] = {}
    console_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        for name, viewport in (
            ("desktop", {"width": 1440, "height": 1000}),
            ("mobile", {"width": 390, "height": 844}),
        ):
            page = browser.new_page(viewport=viewport)
            page.on(
                "console",
                lambda message, label=name: (
                    console_errors.append(f"{label}: {message.type}: {message.text}")
                    if message.type == "error"
                    else None
                ),
            )
            replay(page, turns)
            metrics = verify_page(page)
            assert_metrics(metrics)
            screenshot = OUTPUT / f"skill-rendering-panel-{name}-{stamp}.png"
            page.screenshot(path=str(screenshot), full_page=False)
            result[name] = {**metrics, "screenshot": str(screenshot.relative_to(PROJECT))}
            page.close()
        browser.close()

    if console_errors:
        raise RuntimeError(f"development panel console errors: {console_errors}")
    result["console_errors"] = console_errors
    report_path = OUTPUT / f"skill-rendering-panel-{stamp}.json"
    result["report_path"] = str(report_path.relative_to(PROJECT))
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
