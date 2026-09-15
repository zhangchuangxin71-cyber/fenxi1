from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ThemeColors(StrictModel):
    text: str
    heading: str
    muted: str
    primary: str
    secondary: str
    accent: str
    background: str = "#FFFFFF"
    surface: str
    border: str
    code_background: str = "#F4F4F5"

    @field_validator("*", mode="after")
    @classmethod
    def require_hex_color(cls, value: str) -> str:
        if len(value) not in {4, 7, 9} or not value.startswith("#"):
            raise ValueError("theme colors must use #RGB, #RRGGBB or #RRGGBBAA")
        try:
            int(value[1:], 16)
        except ValueError as exc:
            raise ValueError("theme color contains non-hexadecimal characters") from exc
        return value.upper()


class ThemeTypography(StrictModel):
    body_size: str = "15px"
    body_line_height: str = "1.8"
    h1_size: str = "25px"
    h2_size: str = "19px"
    h3_size: str = "17px"
    caption_size: str = "12px"
    letter_spacing: str = "0"


class ThemeSpacing(StrictModel):
    content_padding: str = "0 16px"
    paragraph_margin: str = "0 0 18px"
    section_margin: str = "44px 0 24px"


class ThemeVariants(StrictModel):
    profile: Literal[
        "professional",
        "minimal",
        "navy",
        "editorial",
        "moyu_green",
        "graphite_minimal",
        "olive_journal",
        "red_white",
    ]
    blockquote: Literal["subtle", "line", "card", "editorial"] = "subtle"
    list: Literal["marker", "numbered", "card"] = "marker"
    code: Literal["light", "dark"] = "light"


class ThemeDefinition(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    version: str
    label: str
    source: str
    suitable_for: list[str]
    colors: ThemeColors
    typography: ThemeTypography = Field(default_factory=ThemeTypography)
    spacing: ThemeSpacing = Field(default_factory=ThemeSpacing)
    variants: ThemeVariants


class ValidationIssue(StrictModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(StrictModel):
    valid: bool
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)
    metrics: dict[str, int | float | str | bool] = Field(default_factory=dict)


class ThemeSelection(StrictModel):
    theme_id: str
    reason: str
    scores: dict[str, int] = Field(default_factory=dict)


class RenderResult(StrictModel):
    final_html: str
    theme_id: str
    renderer_version: str
    validation_report: ValidationReport
    fallback_used: bool = False
    fallback_reason: str | None = None
    selection: ThemeSelection
    timings_ms: dict[str, float] = Field(default_factory=dict)
    source_metadata: dict[str, str] = Field(default_factory=dict)


class LayoutEvent(StrictModel):
    stage: Literal["select_theme", "render_markdown", "validate_html", "fallback"]
    status: Literal["running", "completed", "failed", "degraded"]
    attempt: int = 1
    details: dict[str, Any] = Field(default_factory=dict)


LayoutEventCallback = Callable[[LayoutEvent], None]
