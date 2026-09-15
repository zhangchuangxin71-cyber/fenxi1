from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.schemas import RetrievalCategory


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoutedQueryGroupDraft(StrictModel):
    queries: list[str] = Field(
        description="从原始问题拆分并改写出的独立问题列表；每项必须可单独用于检索。"
    )
    target_docs_description: str = Field(
        description="目标文档的身份、主题或类型描述，不包含待查答案和执行动作。"
    )
    target_docs_keywords: list[str] = Field(
        description="可能出现在目标文档名或数据库摘要中的名称、别名和文档类型关键词。"
    )

    @field_validator("queries", "target_docs_keywords")
    @classmethod
    def normalize_list(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("target_docs_description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return value.strip()


class ScopeDirectGroupDraft(StrictModel):
    queries: list[str] = Field(description="只依赖当前请求文档范围本身即可处理的独立问题列表。")

    @field_validator("queries")
    @classmethod
    def normalize_queries(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class QueryClassificationOutput(StrictModel):
    routed_focused: list[RoutedQueryGroupDraft] = Field(
        description="需要先路由文档、再从少量页面中精确查找证据的问题组。"
    )
    routed_broad: list[RoutedQueryGroupDraft] = Field(
        description="需要先路由文档、再大范围返回原文以供下游总结或比较的问题组。"
    )
    routed_direct: list[RoutedQueryGroupDraft] = Field(
        description="需要先路由文档、再直接访问指定页、章节、目录或已有摘要的问题组。"
    )
    scope_direct: list[ScopeDirectGroupDraft] = Field(
        description="无需路由文档，直接访问当前请求范围元信息或简要概览的问题组。"
    )


class QueryGroup(StrictModel):
    group_ref: str
    category: RetrievalCategory
    queries: list[str]
    target_docs_description: str | None = None
    target_docs_keywords: list[str] = Field(default_factory=list)
    ordinal: int
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class ReferencedQuery:
    query_ref: str
    question: str


@dataclass(frozen=True, slots=True)
class QueryFacets:
    query_ref: str
    is_scope: bool
    is_direct: bool = False
    is_focused: bool = False


@dataclass(frozen=True, slots=True)
class DocumentCluster:
    query_refs: tuple[str, ...]
    target_docs_description: str
    target_docs_keywords: tuple[str, ...]


_CATEGORY_ORDER: tuple[RetrievalCategory, ...] = (
    "routed_focused",
    "routed_broad",
    "routed_direct",
    "scope_direct",
)


def materialize_groups(output: QueryClassificationOutput) -> list[QueryGroup]:
    groups: list[QueryGroup] = []
    for category in _CATEGORY_ORDER:
        for draft in getattr(output, category):
            if not draft.queries:
                continue
            ordinal = len(groups) + 1
            routed = isinstance(draft, RoutedQueryGroupDraft)
            groups.append(
                QueryGroup(
                    group_ref=f"g{ordinal:04d}",
                    category=category,
                    queries=list(draft.queries),
                    target_docs_description=(draft.target_docs_description if routed else None),
                    target_docs_keywords=(list(draft.target_docs_keywords) if routed else []),
                    ordinal=ordinal,
                )
            )
    return groups


ClassificationCategory = Literal["routed_focused", "routed_broad", "routed_direct", "scope_direct"]
