from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from app.rendering.wechat_layout.contracts import ThemeDefinition


class HtmlRendererBackend(Protocol):
    """Backend boundary for deterministic, model-driven, or agent-driven renderers."""

    version: str

    def render(
        self,
        markdown: str,
        images: Sequence[Mapping[str, Any]],
        *,
        theme: ThemeDefinition,
    ) -> tuple[str, str, list[dict[str, str]]]: ...
