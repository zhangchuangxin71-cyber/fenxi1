from __future__ import annotations

from app.agent_engine.skill_loader import SkillLoader
from app.rendering.skill_driven.component_registry import REGISTRY, ComponentRegistry, SkillTheme


class RenderingAssetCatalog:
    """Combines immutable SkillLoader hashes and safe component definitions."""

    def __init__(self, loader: SkillLoader, registry: ComponentRegistry | None = None) -> None:
        self.loader = loader
        self.registry = registry or REGISTRY
        self.metadata = {
            skill_id: loader.metadata(skill_id, node_name="render_html")
            for skill_id in ("gzh_design", "xiaowan_layout")
        }

    def themes(self) -> list[dict[str, object]]:
        return [
            {
                "theme_id": theme.theme_id,
                "label": theme.label,
                "suitable_for": list(theme.suitable_for),
                "description": theme.description,
                "source_reference": theme.source_reference,
                "source_sha256": theme.source_sha256,
                "components": {
                    semantic: {
                        "component_id": component.component_id,
                        "label": component.label,
                    }
                    for semantic, component in theme.components.items()
                },
            }
            for theme in self.registry.all()
        ]

    def theme(self, theme_id: str) -> SkillTheme:
        return self.registry.theme(theme_id)

    def components(self, theme_id: str) -> list[dict[str, object]]:
        theme = self.registry.theme(theme_id)
        result: list[dict[str, object]] = [
            {
                "component_id": component.component_id,
                "semantic": component.semantic,
                "label": component.label,
                "source_reference": component.source_reference,
            }
            for variants in theme.component_variants.values()
            for component in variants
        ]
        result.append(
            {
                "component_id": "common.code-dark",
                "semantic": "code",
                "label": "通用深色代码块",
                "source_reference": "gzh_design/references/common-components.md",
            }
        )
        return result
