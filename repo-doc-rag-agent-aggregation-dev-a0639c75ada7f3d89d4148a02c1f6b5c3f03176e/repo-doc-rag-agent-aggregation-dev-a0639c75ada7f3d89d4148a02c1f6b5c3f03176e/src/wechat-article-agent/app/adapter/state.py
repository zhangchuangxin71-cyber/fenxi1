from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PendingInteraction:
    runtime_interrupt_id: str
    interrupt_id: str
    response_id: str
    artifact_id: str
    revision: int
    stage: str
    form: dict[str, Any]
    raw: dict[str, Any]


def pending_interaction(state: Mapping[str, Any] | None) -> PendingInteraction | None:
    if not state:
        return None
    interrupts = state.get("interrupts") or []
    if not isinstance(interrupts, list) or not interrupts:
        return None
    entry = interrupts[0]
    if not isinstance(entry, Mapping):
        return None
    value = entry.get("value")
    if not isinstance(value, Mapping):
        return None
    required = ("interrupt_id", "response_id", "artifact_id", "revision", "stage", "form")
    if any(name not in value for name in required):
        return None
    return PendingInteraction(
        runtime_interrupt_id=str(entry.get("id") or ""),
        interrupt_id=str(value["interrupt_id"]),
        response_id=str(value["response_id"]),
        artifact_id=str(value["artifact_id"]),
        revision=int(value["revision"]),
        stage=str(value["stage"]),
        form=dict(value["form"]),
        raw=dict(value),
    )


def state_values(state: Mapping[str, Any] | None) -> dict[str, Any]:
    if not state:
        return {}
    values = state.get("values")
    return dict(values) if isinstance(values, Mapping) else {}


def artifact_stage_for_workflow_stage(stage: str) -> str | None:
    if stage == "docs_research_clarification":
        return "material_library"
    if stage == "material_conflict_review":
        return "material_conflicts"
    if stage == "task_spec_review":
        return "task_spec"
    if stage == "outline_review":
        return "outline"
    if stage == "article_review":
        return "article_markdown"
    if stage == "completed":
        return "final_html"
    return None
