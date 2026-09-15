from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.rendering.wechat_layout.contracts import ThemeDefinition
from app.rendering.wechat_layout.errors import ThemeRegistryError

THEMES_DIR = Path(__file__).parent


class ThemeRegistry:
    """Loads the bundled allowlisted themes and rejects malformed definitions."""

    def __init__(self, themes_dir: Path = THEMES_DIR) -> None:
        self._themes_dir = themes_dir
        self._themes = self._load()

    def _load(self) -> dict[str, ThemeDefinition]:
        themes: dict[str, ThemeDefinition] = {}
        for path in sorted(self._themes_dir.glob("*.yaml")):
            try:
                value = ThemeDefinition.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
            except (OSError, yaml.YAMLError, ValidationError) as exc:
                raise ThemeRegistryError(f"Invalid theme definition {path.name}: {exc}") from exc
            if value.id in themes:
                raise ThemeRegistryError(f"Duplicate theme id: {value.id}")
            themes[value.id] = value
        if not themes:
            raise ThemeRegistryError(f"No themes found under {self._themes_dir}")
        return themes

    def get(self, theme_id: str) -> ThemeDefinition:
        try:
            return self._themes[theme_id]
        except KeyError as exc:
            raise ThemeRegistryError(f"Unknown layout theme: {theme_id}") from exc

    def ids(self) -> tuple[str, ...]:
        return tuple(self._themes)

    def all(self) -> tuple[ThemeDefinition, ...]:
        return tuple(self._themes.values())


@lru_cache(maxsize=1)
def default_registry() -> ThemeRegistry:
    return ThemeRegistry()
