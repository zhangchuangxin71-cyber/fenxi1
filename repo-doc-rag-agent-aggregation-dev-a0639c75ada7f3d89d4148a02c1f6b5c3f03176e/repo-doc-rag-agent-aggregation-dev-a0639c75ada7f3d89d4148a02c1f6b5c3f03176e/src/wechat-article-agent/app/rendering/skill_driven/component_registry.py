from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import cast


@dataclass(frozen=True, slots=True)
class SkillComponent:
    component_id: str
    semantic: str
    label: str
    source_reference: str


@dataclass(frozen=True, slots=True)
class SkillTheme:
    theme_id: str
    label: str
    suitable_for: tuple[str, ...]
    description: str
    source_reference: str
    colors: MappingProxyType[str, str]
    components: MappingProxyType[str, SkillComponent]
    component_variants: MappingProxyType[str, tuple[SkillComponent, ...]]
    source_sha256: str


_SKILL_ROOT = Path(__file__).parents[2] / "skills" / "rendering" / "gzh_design"


_THEME_DATA: dict[str, dict[str, object]] = {
    "moyu-green": {
        "label": "摸鱼绿",
        "suitable": ("教程", "清单", "工具盘点", "轻松科普"),
        "description": "杂志快讯封面、横向导读、绿色信息卡和高密度实用组件。",
        "colors": {
            "primary": "#059669",
            "deep": "#064E3B",
            "soft": "#ECFDF5",
            "pale": "#D1FAE5",
            "accent": "#F59E0B",
            "text": "#1F2937",
            "muted": "#6B7280",
            "line": "#A7F3D0",
            "paper": "#FFFFFF",
        },
        "components": {
            "hero": ("cover-breaking", "杂志快讯封面"),
            "toc": ("toc-scroll", "横向导读卡"),
            "heading": ("chapter-title", "章节编号标题"),
            "body": ("paragraph", "标准正文"),
            "quote": ("highlight-quote", "核心观点引用"),
            "list": ("step-list", "步骤清单"),
            "emphasis": ("key-point-card", "重点信息卡"),
            "image": ("image-card", "圆角图片卡"),
            "closing": ("end-mark", "主题结束标记"),
        },
    },
    "red-white": {
        "label": "红白色系",
        "suitable": ("深度分析", "政策解读", "观点", "公共议题"),
        "description": "红色编号、编辑式导读、数据卡与克制有力的重点强调。",
        "colors": {
            "primary": "#DC2626",
            "deep": "#991B1B",
            "soft": "#FEF2F2",
            "pale": "#FEE2E2",
            "accent": "#111827",
            "text": "#374151",
            "muted": "#9CA3AF",
            "line": "#FECACA",
            "paper": "#FFFFFF",
        },
        "components": {
            "hero": ("editorial-quote-hero", "编辑式引言首屏"),
            "toc": ("toc-grid", "红白导读卡"),
            "heading": ("numbered-heading", "红色编号章节"),
            "body": ("editorial-paragraph", "编辑式正文"),
            "quote": ("red-quote", "红色引用块"),
            "list": ("numbered-list", "编号要点列表"),
            "emphasis": ("data-point-card", "数据重点卡"),
            "image": ("image-frame", "红线图片框"),
            "closing": ("end-divider", "END 分割线"),
        },
    },
    "graphite-minimal": {
        "label": "石墨极简",
        "suitable": ("科技评论", "专业观点", "设计", "高端品牌"),
        "description": "石墨细线、超大留白、水印编号和理性编辑骨架。",
        "colors": {
            "primary": "#52525B",
            "deep": "#27272A",
            "soft": "#FAFAFA",
            "pale": "#F4F4F5",
            "accent": "#F97316",
            "text": "#52525B",
            "muted": "#A1A1AA",
            "line": "#E4E4E7",
            "paper": "#FFFFFF",
        },
        "components": {
            "hero": ("line-quote-hero", "上下细线引言首屏"),
            "toc": ("wire-toc", "极简线框导读"),
            "heading": ("watermark-heading", "水印编号章节"),
            "body": ("graphite-paragraph", "石墨正文"),
            "quote": ("line-quote", "细线引用"),
            "list": ("minimal-list", "极简清单"),
            "emphasis": ("insight-card", "洞察卡"),
            "image": ("minimal-image", "极简图片框"),
            "closing": ("geometry-end", "几何结束标记"),
        },
    },
    "zen-whitespace": {
        "label": "留白禅意",
        "suitable": ("深度随笔", "艺术", "生活方式", "人物故事"),
        "description": "衬线标题、墨绿细线、克制卡片和强呼吸感。",
        "colors": {
            "primary": "#4A5D52",
            "deep": "#2F3E36",
            "soft": "#F7F9F7",
            "pale": "#EEF3EF",
            "accent": "#A67C52",
            "text": "#4B514D",
            "muted": "#9AA39D",
            "line": "#DCE5DF",
            "paper": "#FFFFFF",
        },
        "components": {
            "hero": ("serif-hero", "禅意衬线首屏"),
            "toc": ("breathing-toc", "留白导读"),
            "heading": ("serif-heading", "墨绿衬线章节"),
            "body": ("zen-paragraph", "留白正文"),
            "quote": ("breathing-quote", "留白引用"),
            "list": ("quiet-list", "安静清单"),
            "emphasis": ("calm-note", "静谧重点卡"),
            "image": ("paper-image", "纸张图片框"),
            "closing": ("zen-end", "禅意结束标记"),
        },
    },
    "moyu-ticket": {
        "label": "摸鱼票据",
        "suitable": ("测评", "工具对比", "创意盘点", "热点科普"),
        "description": "票据封面、硬阴影、锯齿语义、编号标签和强记忆点。",
        "colors": {
            "primary": "#059669",
            "deep": "#064E3B",
            "soft": "#ECFDF5",
            "pale": "#D1FAE5",
            "accent": "#FBBF24",
            "text": "#1F2937",
            "muted": "#6B7280",
            "line": "#111827",
            "paper": "#FFFEF7",
        },
        "components": {
            "hero": ("ticket-cover", "票据封面"),
            "toc": ("ticket-stubs", "票根导读"),
            "heading": ("ticket-heading", "票据章节标题"),
            "body": ("ticket-paragraph", "票据正文"),
            "quote": ("conclusion-ticket", "结论票据"),
            "list": ("numbered-feature-list", "编号特征列表"),
            "emphasis": ("key-point-ticket", "核心观点票"),
            "image": ("image-ticket", "票框图片"),
            "closing": ("ticket-end", "票据结束符"),
        },
    },
    "olive-journal": {
        "label": "橄榄手记",
        "suitable": ("案例复盘", "系统说明", "内刊", "深度评测"),
        "description": "内刊头版、黑色标题条、橙色标签和多层摘要组件。",
        "colors": {
            "primary": "#1E1F23",
            "deep": "#111214",
            "soft": "#F4F3EE",
            "pale": "#E8E5DA",
            "accent": "#ED7B2F",
            "text": "#34363A",
            "muted": "#8A8C90",
            "line": "#D8D4C8",
            "paper": "#FCFBF7",
        },
        "components": {
            "hero": ("hero-card", "内刊头图卡"),
            "toc": ("frontpage-summary-strip", "头版摘要条"),
            "heading": ("section-title", "内刊分节标题"),
            "body": ("richtext-paragraph", "内刊正文"),
            "quote": ("editors-note", "编者按"),
            "list": ("item-list-card", "条目列表卡"),
            "emphasis": ("key-point-card", "重点观点卡"),
            "image": ("image-card", "内刊图片卡"),
            "closing": ("ending-content", "内刊收束块"),
        },
    },
}

_EXTRA_COMPONENTS: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {
    "moyu-green": {
        "hero": (("cover-dashboard", "数据仪表封面"),),
        "toc": (("toc-cards", "卡片导读"),),
        "heading": (("label-heading", "标签章节标题"),),
        "emphasis": (("tip-card", "实用提示卡"), ("metric-card", "数字重点卡")),
    },
    "red-white": {
        "hero": (("statement-hero", "观点宣言首屏"),),
        "toc": (("toc-lines", "红线导读"),),
        "heading": (("statement-heading", "观点式章节标题"),),
        "emphasis": (("warning-card", "红色警示卡"), ("quote-card", "观点摘录卡")),
    },
    "graphite-minimal": {
        "hero": (("numbered-hero", "编号编辑首屏"),),
        "toc": (("index-toc", "索引式导读"),),
        "heading": (("line-heading", "细线章节标题"),),
        "emphasis": (("border-note", "边框注记"), ("orange-anchor", "橙色锚点卡")),
    },
    "zen-whitespace": {
        "hero": (("quiet-hero", "纯留白首屏"),),
        "toc": (("vertical-toc", "纵向留白导读"),),
        "heading": (("brush-heading", "短线章节标题"),),
        "emphasis": (("serif-quote-card", "衬线观点卡"), ("soft-note", "柔和旁注")),
    },
    "moyu-ticket": {
        "hero": (("boarding-pass-cover", "登机牌封面"),),
        "toc": (("coupon-toc", "优惠券导读"),),
        "heading": (("stamp-heading", "印章章节标题"),),
        "emphasis": (("rating-ticket", "评分票据"), ("coupon-card", "优惠券重点卡")),
    },
    "olive-journal": {
        "hero": (("masthead-hero", "内刊报头首屏"),),
        "toc": (("dark-summary-split", "暗色摘要导读"),),
        "heading": (("issue-heading", "期号章节标题"),),
        "emphasis": (("editors-note", "编者按卡"), ("compare-summary", "对比摘要卡")),
    },
}


class ComponentRegistry:
    """Immutable project-safe projection of the reviewed GZH component libraries."""

    def __init__(self) -> None:
        themes: dict[str, SkillTheme] = {}
        for theme_id, raw in _THEME_DATA.items():
            reference = f"references/theme-{theme_id}.md"
            source = (_SKILL_ROOT / reference).read_bytes()
            components: dict[str, SkillComponent] = {}
            variants: dict[str, tuple[SkillComponent, ...]] = {}
            raw_components = cast(dict[str, tuple[str, str]], raw["components"])
            for semantic, value in raw_components.items():
                short_id, label = value
                component_id = f"{theme_id}.{short_id}"
                components[semantic] = SkillComponent(
                    component_id=component_id,
                    semantic=semantic,
                    label=label,
                    source_reference=reference,
                )
                extra = _EXTRA_COMPONENTS.get(theme_id, {}).get(semantic, ())
                variants[semantic] = (
                    components[semantic],
                    *(
                        SkillComponent(
                            component_id=f"{theme_id}.{extra_id}",
                            semantic=semantic,
                            label=extra_label,
                            source_reference=reference,
                        )
                        for extra_id, extra_label in extra
                    ),
                )
            themes[theme_id] = SkillTheme(
                theme_id=theme_id,
                label=str(raw["label"]),
                suitable_for=tuple(cast(tuple[str, ...], raw["suitable"])),
                description=str(raw["description"]),
                source_reference=reference,
                colors=MappingProxyType(cast(dict[str, str], raw["colors"])),
                components=MappingProxyType(components),
                component_variants=MappingProxyType(variants),
                source_sha256=hashlib.sha256(source).hexdigest(),
            )
        self._themes = MappingProxyType(themes)

    def theme(self, theme_id: str) -> SkillTheme:
        try:
            return self._themes[theme_id]
        except KeyError as exc:
            raise KeyError(f"Unknown Skill theme: {theme_id}") from exc

    def all(self) -> tuple[SkillTheme, ...]:
        return tuple(self._themes.values())

    def ids(self) -> tuple[str, ...]:
        return tuple(self._themes)

    def component_ids(self) -> tuple[str, ...]:
        return tuple(
            component.component_id
            for theme in self._themes.values()
            for variants in theme.component_variants.values()
            for component in variants
        )

    def component(self, component_id: str) -> SkillComponent:
        theme_id = component_id.split(".", 1)[0]
        theme = self.theme(theme_id)
        for variants in theme.component_variants.values():
            for component in variants:
                if component.component_id == component_id:
                    return component
        raise KeyError(f"Unknown Skill component: {component_id}")


REGISTRY = ComponentRegistry()
