from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.rendering.skill_driven.component_registry import ComponentRegistry
from app.rendering.skill_driven.contracts import MarkdownLayoutDocument, SkillLayoutPlan
from app.rendering.skill_driven.markdown_model import node_map

REQUIRED_SEMANTICS = (
    "hero",
    "toc",
    "heading",
    "body",
    "quote",
    "list",
    "emphasis",
    "image",
    "closing",
)


def validate_plan(
    plan: SkillLayoutPlan,
    *,
    document: MarkdownLayoutDocument,
    images: Sequence[Mapping[str, object]],
    registry: ComponentRegistry,
) -> list[str]:
    errors: list[str] = []
    try:
        theme = registry.theme(plan.theme_id)
    except KeyError:
        return [f"unknown theme_id: {plan.theme_id}"]

    component_fields = {
        "hero": plan.hero_component_id,
        "toc": plan.toc_component_id,
        "body": plan.body_component_id,
        "quote": plan.quote_component_id,
        "list": plan.list_component_id,
        "emphasis": plan.emphasis_component_id,
        "image": plan.image_component_id,
        "closing": plan.closing_component_id,
    }
    for semantic, component_id in component_fields.items():
        allowed_components = {item.component_id for item in theme.component_variants[semantic]}
        if component_id not in allowed_components:
            errors.append(f"{semantic}_component_id must be one of {sorted(allowed_components)}")

    expected_sections = [section.section_id for section in document.sections]
    actual_sections = [section.section_id for section in plan.sections]
    if actual_sections != expected_sections:
        errors.append(f"sections must exactly cover {expected_sections} in order")
    by_id = {section.section_id: section for section in document.sections}
    all_nodes = node_map(document)
    accent_count = 0
    for section in plan.sections:
        expected_headings = {item.component_id for item in theme.component_variants["heading"]}
        if section.heading_component_id not in expected_headings:
            errors.append(
                f"heading_component_id for {section.section_id} must be one of {sorted(expected_headings)}"
            )
        source_section = by_id.get(section.section_id)
        allowed = {
            node.node_id
            for node in (source_section.nodes if source_section is not None else [])
            if node.kind in {"paragraph", "blockquote", "list"}
        }
        if len(section.accent_node_ids) != len(set(section.accent_node_ids)):
            errors.append(f"accent_node_ids contains duplicates in {section.section_id}")
        for node_id in section.accent_node_ids:
            if node_id not in all_nodes or node_id not in allowed:
                errors.append(f"invalid accent node {node_id} in {section.section_id}")
        accent_count += len(section.accent_node_ids)

    paragraph_count = sum(
        1 for section in document.sections for node in section.nodes if node.kind == "paragraph"
    )
    max_accents = max(2, min(6, paragraph_count // 3 + 1))
    if accent_count > max_accents:
        errors.append(f"accent node count {accent_count} exceeds Xiaowan budget {max_accents}")
    if document.sections and accent_count == 0:
        errors.append("plan is too thin: select at least one existing content node for emphasis")
    if len(component_fields) < 8 or len(plan.sections) != len(document.sections):
        errors.append("SKILL_LAYOUT_PLAN_TOO_THIN")
    if images and not plan.image_component_id:
        errors.append("image component is required when image artifacts exist")
    return errors
