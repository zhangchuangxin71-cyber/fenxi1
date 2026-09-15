from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.llm.inputs import LayoutThemeSelectionInput, render_input
from app.llm.schemas import dynamic_theme_selection_output
from app.prompts.system import build_layout_theme_selection_system_prompt
from app.rendering.wechat_layout.catalog import describe_supported_themes
from app.rendering.wechat_layout.themes.registry import ThemeRegistry, default_registry

ModelEventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


async def select_theme_with_llm(
    *,
    llm: Any,
    run_id: str,
    task_spec: dict[str, Any],
    event_callback: ModelEventCallback | None = None,
    registry: ThemeRegistry | None = None,
    theme_catalog: str | None = None,
) -> tuple[Any, str]:
    """Select one registered theme with a strict, registry-derived schema."""

    active_registry = registry or default_registry()
    theme_ids = list(active_registry.ids())
    catalog = theme_catalog or describe_supported_themes(active_registry)
    output_model = dynamic_theme_selection_output("LayoutThemeSelectionOutput", theme_ids)
    result = await llm.structured(
        run_id=run_id,
        phase="html_layout_theme",
        system_prompt=build_layout_theme_selection_system_prompt(catalog),
        input_text=render_input(LayoutThemeSelectionInput.model_validate({"task_spec": task_spec})),
        output_model=output_model,
        event_callback=event_callback,
        thinking=False,
    )
    # The dynamic enum is enforced by Pydantic; this lookup also protects the
    # renderer if a custom gateway returns an object without validation.
    active_registry.get(str(result.theme_id))
    return result, catalog
