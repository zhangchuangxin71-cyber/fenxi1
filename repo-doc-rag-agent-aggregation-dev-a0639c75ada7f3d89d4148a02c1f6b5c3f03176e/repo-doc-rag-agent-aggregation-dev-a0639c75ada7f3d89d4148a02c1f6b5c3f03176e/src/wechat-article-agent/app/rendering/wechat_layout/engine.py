from __future__ import annotations

from collections.abc import Mapping, Sequence
from time import perf_counter
from typing import Any

from app.rendering.wechat_layout.contracts import (
    LayoutEvent,
    LayoutEventCallback,
    RenderResult,
    ThemeSelection,
    ValidationIssue,
    ValidationReport,
)
from app.rendering.wechat_layout.errors import LayoutError, ThemeRegistryError
from app.rendering.wechat_layout.legacy import LegacyHtmlRendererAdapter
from app.rendering.wechat_layout.protocols import HtmlRendererBackend
from app.rendering.wechat_layout.renderer import DeterministicHtmlRenderer
from app.rendering.wechat_layout.selector import DEFAULT_THEME, ThemeSelector
from app.rendering.wechat_layout.themes.registry import ThemeRegistry, default_registry
from app.rendering.wechat_layout.validator import HtmlValidator


class LayoutEngine:
    """Theme selection, rendering, validation, and fallback orchestration."""

    def __init__(
        self,
        *,
        registry: ThemeRegistry | None = None,
        default_theme: str = DEFAULT_THEME,
        renderer: HtmlRendererBackend | None = None,
        validator: HtmlValidator | None = None,
        legacy: LegacyHtmlRendererAdapter | None = None,
    ) -> None:
        self._registry = registry or default_registry()
        self._default_theme = default_theme
        self._selector = ThemeSelector(self._registry, default_theme)
        self._renderer = renderer or DeterministicHtmlRenderer()
        self._validator = validator or HtmlValidator()
        self._legacy = legacy or LegacyHtmlRendererAdapter()

    def render(
        self,
        *,
        markdown: str,
        images: Sequence[Mapping[str, Any]] | None,
        task_spec: Mapping[str, Any] | None,
        requested_theme_id: str | None = None,
        event_callback: LayoutEventCallback | None = None,
    ) -> RenderResult:
        normalized_images = tuple(images or ())
        fallback_used = False
        fallback_reason: str | None = None
        timings: dict[str, float] = {}

        self._event(event_callback, "select_theme", "running")
        select_started = perf_counter()
        try:
            selection = self._selector.select(
                requested_theme_id=requested_theme_id,
                task_spec=task_spec,
            )
        except ThemeRegistryError as exc:
            fallback_used = True
            fallback_reason = str(exc)
            selection = ThemeSelection(
                theme_id=self._default_theme,
                reason="invalid_requested_theme",
                scores={self._default_theme: 1},
            )
            self._event(
                event_callback,
                "fallback",
                "degraded",
                details={"from": requested_theme_id, "to": self._default_theme, "reason": str(exc)},
            )
        timings["select_theme"] = self._elapsed(select_started)
        self._event(
            event_callback,
            "select_theme",
            "completed",
            details={"theme_id": selection.theme_id, "reason": selection.reason},
        )

        attempts = [selection.theme_id]
        if selection.theme_id != self._default_theme:
            attempts.append(self._default_theme)
        errors: list[str] = []
        for attempt, theme_id in enumerate(attempts, start=1):
            try:
                html, report, attempt_timings = self._render_attempt(
                    markdown=markdown,
                    images=normalized_images,
                    theme_id=theme_id,
                    attempt=attempt,
                    event_callback=event_callback,
                )
            except Exception as exc:
                errors.append(f"{theme_id}: {type(exc).__name__}: {exc}")
                if theme_id != self._default_theme:
                    fallback_used = True
                    fallback_reason = errors[-1]
                    self._event(
                        event_callback,
                        "fallback",
                        "degraded",
                        attempt=attempt,
                        details={"from": theme_id, "to": self._default_theme, "reason": errors[-1]},
                    )
                continue
            timings.update({f"attempt_{attempt}_{key}": value for key, value in attempt_timings.items()})
            if theme_id != selection.theme_id:
                fallback_used = True
                fallback_reason = fallback_reason or "; ".join(errors)
            return RenderResult(
                final_html=html,
                theme_id=theme_id,
                renderer_version=self._renderer.version,
                validation_report=report,
                fallback_used=fallback_used,
                fallback_reason=fallback_reason,
                selection=selection,
                timings_ms=timings,
                source_metadata={
                    "skill": "wechat-layout",
                    "theme_source": self._registry.get(theme_id).source,
                },
            )

        self._event(
            event_callback,
            "fallback",
            "running",
            attempt=len(attempts) + 1,
            details={"to": "legacy-safe", "reason": "; ".join(errors)},
        )
        try:
            started = perf_counter()
            html, source_text, inserted = self._legacy.render(markdown, normalized_images)
            timings["legacy_render"] = self._elapsed(started)
            started = perf_counter()
            report = self._validator.validate(
                html,
                source_visible_text=source_text,
                expected_images=normalized_images,
            )
            timings["legacy_validate"] = self._elapsed(started)
            self._add_position_warnings(report, inserted)
            if not report.valid:
                raise LayoutError(self._validation_message(report))
        except Exception as exc:
            self._event(
                event_callback,
                "fallback",
                "failed",
                attempt=len(attempts) + 1,
                details={"renderer": "legacy-safe", "error": str(exc)},
            )
            raise LayoutError("All layout renderers failed: " + "; ".join([*errors, str(exc)])) from exc

        self._event(
            event_callback,
            "fallback",
            "completed",
            attempt=len(attempts) + 1,
            details={"renderer": "legacy-safe"},
        )
        return RenderResult(
            final_html=html,
            theme_id="legacy-safe",
            renderer_version=self._legacy.version,
            validation_report=report,
            fallback_used=True,
            fallback_reason="; ".join(errors),
            selection=selection,
            timings_ms=timings,
            source_metadata={"skill": "wechat-layout", "theme_source": "project legacy renderer"},
        )

    def _render_attempt(
        self,
        *,
        markdown: str,
        images: Sequence[Mapping[str, Any]],
        theme_id: str,
        attempt: int,
        event_callback: LayoutEventCallback | None,
    ) -> tuple[str, ValidationReport, dict[str, float]]:
        theme = self._registry.get(theme_id)
        timings: dict[str, float] = {}
        self._event(
            event_callback,
            "render_markdown",
            "running",
            attempt=attempt,
            details={"theme_id": theme_id, "markdown_chars": len(markdown), "images": len(images)},
        )
        started = perf_counter()
        try:
            html, source_text, inserted = self._renderer.render(markdown, images, theme=theme)
        except Exception as exc:
            self._event(
                event_callback,
                "render_markdown",
                "failed",
                attempt=attempt,
                details={"theme_id": theme_id, "error": str(exc)},
            )
            raise
        timings["render_markdown"] = self._elapsed(started)
        self._event(
            event_callback,
            "render_markdown",
            "completed",
            attempt=attempt,
            details={"theme_id": theme_id, "html_chars": len(html), "inserted_images": inserted},
        )

        self._event(
            event_callback,
            "validate_html",
            "running",
            attempt=attempt,
            details={"theme_id": theme_id},
        )
        started = perf_counter()
        report = self._validator.validate(
            html,
            source_visible_text=source_text,
            expected_images=images,
        )
        self._add_position_warnings(report, inserted)
        timings["validate_html"] = self._elapsed(started)
        status = "completed" if report.valid else "failed"
        self._event(
            event_callback,
            "validate_html",
            status,
            attempt=attempt,
            details={
                "theme_id": theme_id,
                "valid": report.valid,
                "errors": [issue.model_dump() for issue in report.errors],
                "warnings": [issue.model_dump() for issue in report.warnings],
                "metrics": report.metrics,
            },
        )
        if not report.valid:
            raise LayoutError(self._validation_message(report))
        return html, report, timings

    @staticmethod
    def _add_position_warnings(report: ValidationReport, inserted: list[dict[str, str]]) -> None:
        unmatched = [item for item in inserted if item.get("matched") != "exact"]
        if unmatched:
            report.warnings.append(
                ValidationIssue(
                    code="IMAGE_POSITION_FALLBACK",
                    message="One or more images used a deterministic fallback insertion position.",
                    details={"images": unmatched},
                )
            )
            report.metrics["image_position_fallbacks"] = len(unmatched)

    @staticmethod
    def _validation_message(report: ValidationReport) -> str:
        return ", ".join(f"{issue.code}: {issue.message}" for issue in report.errors)

    @staticmethod
    def _elapsed(started: float) -> float:
        return round((perf_counter() - started) * 1000, 3)

    @staticmethod
    def _event(
        callback: LayoutEventCallback | None,
        stage: str,
        status: str,
        *,
        attempt: int = 1,
        details: dict[str, Any] | None = None,
    ) -> None:
        if callback is None:
            return
        callback(
            LayoutEvent.model_validate(
                {
                    "stage": stage,
                    "status": status,
                    "attempt": attempt,
                    "details": details or {},
                }
            )
        )
