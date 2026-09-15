from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from app.api.schemas import RetrievalWarning
from app.llm.gateway import LLMCallError, LLMGateway, LLMRequest, LLMResult
from app.workflows.classification.models import (
    QueryClassificationOutput,
    QueryGroup,
    RoutedQueryGroupDraft,
    ScopeDirectGroupDraft,
    materialize_groups,
)
from app.workflows.keyword_ranking import tokenize_query

CLASSIFICATION_SYSTEM_PROMPT = """# 角色与任务
你是检索流程分类器。你负责拆分用户的复合问题，并按后续检索流程分组；你不负责检索，也不回答问题。

# 输入说明
用户消息会给出当前请求范围包含的文档数量和原始问题。文档数量只用于理解“这篇”“上述所有”等范围指代，不能据此编造文档身份。

# 拆分规则
- 重写 query 时，必须是从原 query 拆分并改写后的独立问题。
- 必须保留原问题中的数量约束，例如“列举 10 篇”不得改写成没有数量的“列举文档”。
- 不得新增用户未提出的问题，不得回答问题。
- 跨文档精确事实比较必须拆成可分别对单篇文档检索的独立问题，并保留原比较目标所需的事实。
- 同一组内的问题没有主次；只有类别相同且目标文档相同的问题才能放在同一组。
- 不同下游类别的问题绝对不得放进同一 group。

# 分类标准
- routed_focused：用户意图是从少量明确目标文档中查找少量原文片段。适合答案只依赖局部证据，
  需要根据问题语义精确查找事实、数值、日期、比例、人物、结论或流程的场景。即使请求范围
  包含很多文档，只要用户实际询问的是其中少量明确目标文档，也属于此类。
- routed_broad：用户意图是查看少量明确目标文档，但答案不能由少量局部片段充分支持，而需要
  较大范围的原文覆盖。适合详细总结、深入解释整篇文档、分析文档整体方法，或比较若干明确
  目标文档的整体内容。区别于 routed_focused：routed_focused 只需少量精确片段，
  routed_broad 需要覆盖目标文档的大范围原文。
- routed_direct：用户意图是读取少量明确目标文档在数据库中已经结构化保存的特定资源，不需要
  根据问题语义搜索正文。适合读取页数、章节数、指定页、指定章节、目录、章节结构或数据库已有
  文档摘要。区别于 routed_focused：routed_direct 是按明确资源位置或字段直接读取；
  routed_focused 是根据问题语义搜索未知位置的原文片段。数据库摘要只提供简单概览；深入总结、
  解释或比较明确文档时必须归入 routed_broad。
- scope_direct：用户关注的是当前请求范围内全部可见文档组成的集合，而不是其中少量明确目标文档。
  适合集合级可见性、文档数量、文档列举，或利用数据库已有文档摘要对整个集合做简要概览、主题
  归纳。例如“这 1000 篇毕业论文的研究方向主要有哪些”属于此类。scope_direct 不需要先确定少量
  候选文档，也不进行页面级语义检索。
- 如果用户要求在全部文档中逐篇查找必须依赖正文才能确定的事实，例如“这些文档中哪些公司的
  营业收入超过一亿元”，不能因为涉及全部文档而归入 scope_direct；这仍然属于需要逐文档检索
  原文的 routed_focused。
- routed_direct 与 routed_broad 都要求先确定少量明确目标文档；scope_direct 则针对当前请求范围
  这个集合本身。不得仅因为问题中出现“文档”或“总结”就忽略目标范围和所需证据粒度。
- 命名公司或命名文档中的事实问题不是 scope_direct，即使它与范围元信息问题出现在同一原始 query 中。

# 文档路由参数
- target_docs_description 只描述目标文档身份、主题或类型，不写页码、章节、待查答案或执行动作。
- target_docs_keywords 只包含可能出现在文档名或数据库摘要中的名称、别名、公司名和文档类型词。
- 资源位置和待查内容不能作为文档关键词。

# 示例
- “你能看到哪些文档”属于 scope_direct。
- “简要总结上述所有文档”属于 scope_direct；“详细解释上述所有文档”属于 routed_broad。
- “这些文档主要讲了什么”属于 scope_direct，不得路由文档或搜索页面。
- “详细总结某文档”属于 routed_broad，不得因为数据库存在摘要而归入 routed_direct。
- “A 公司去年营业额是多少”属于 routed_focused。
- “A 与 B 去年营业额谁多”应在 routed_focused 中拆成分别检索 A、B 营业额的独立问题。
- “查看年报第 12 页”属于 routed_direct。
- “这篇文档有几页”属于 routed_direct，不属于 scope_direct。
- “详细比较两份年报的风险章节”属于 routed_broad。

# 输出约束
严格遵守结构化输出契约。四个类别始终存在，没有对应问题时返回空列表。
不要输出 group_ref、query_id、depends_on、reason、置信度或答案。
"""


class ClassificationStrategy(Protocol):
    async def classify(
        self, *, request_id: str, query: str | list[str], scope_document_count: int = 0
    ) -> QueryClassificationOutput: ...


def classification_schema() -> dict[str, Any]:
    schema = QueryClassificationOutput.model_json_schema()
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "query_classification",
            "strict": True,
            "schema": schema,
        },
    }


def query_as_text(query: str | list[str]) -> str:
    return query if isinstance(query, str) else "；".join(query)


class FastLLMClassificationStrategy:
    def __init__(self, *, gateway: LLMGateway, model: str, max_tokens: int) -> None:
        self.gateway = gateway
        self.model = model
        self.max_tokens = max(1, int(max_tokens))

    async def classify(
        self, *, request_id: str, query: str | list[str], scope_document_count: int = 0
    ) -> QueryClassificationOutput:
        query_text = query_as_text(query)
        request = LLMRequest(
            request_id=request_id,
            phase="classification",
            messages=[
                {"role": "system", "content": CLASSIFICATION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"当前用户请求范围包含 {scope_document_count} 篇文档。\n"
                        f"用户原始问题：{query_text}"
                    ),
                },
            ],
            model=self.model,
            max_tokens=self.max_tokens,
            response_format=classification_schema(),
        )
        result = await self.gateway.complete_json(request)
        try:
            return QueryClassificationOutput.model_validate(result.data)
        except ValidationError as exc:
            raise LLMCallError(
                "classification schema validation failed", error_category="invalid_response"
            ) from exc


# Kept as an import-compatible alias while callers migrate to the speed-priority name.
LLMClassificationStrategy = FastLLMClassificationStrategy


_SCOPE_VISIBILITY_PATTERNS = (
    "你能看到哪些文档",
    "你能看到几篇文档",
    "能看到几篇文档",
    "能查到几篇文档",
    "能查到哪些文档",
    "你能查到哪些文档",
    "有几篇文档",
    "有几份文档",
    "当前文档有哪些",
    "列出文档",
    "文档列表",
)
_SCOPE_COLLECTION_PATTERNS = (
    "这几个文档",
    "这些文档",
    "上述文档",
    "上述所有文档",
    "所有文档",
    "全部文档",
)
_SCOPE_OVERVIEW_PATTERNS = (
    "简要总结",
    "简要概览",
    "主要讲",
    "主要内容",
    "大致讲",
    "概览",
)
_DIRECT_PATTERNS = ("目录", "章节结构", "标题结构", "有几页", "页数", "多少页", "有几章")
_BROAD_PATTERNS = ("详细总结", "详细地总结", "全面总结", "解释这篇", "对比", "比较", "差别", "区别")


def _is_direct_query(text: str) -> bool:
    if any(marker in text for marker in _DIRECT_PATTERNS):
        return True
    return bool(re.search(r"第\s*[0-9零一二两三四五六七八九十]+\s*[页章]", text))


def _is_scope_query(text: str) -> bool:
    if any(marker in text for marker in _SCOPE_VISIBILITY_PATTERNS):
        return True
    return any(marker in text for marker in _SCOPE_COLLECTION_PATTERNS) and any(
        marker in text for marker in _SCOPE_OVERVIEW_PATTERNS
    )


def _unambiguous_category(text: str) -> str | None:
    if any(marker in text for marker in _BROAD_PATTERNS):
        return "routed_broad"
    if _is_scope_query(text):
        return "scope_direct"
    if _is_direct_query(text):
        return "routed_direct"
    return None


def _split_query(query: str) -> list[str]:
    parts = [part.strip(" \t\r\n;；。！？?!") for part in re.split(r"[\r\n;；。！？?!]+", query)]
    return list(dict.fromkeys(part for part in parts if part)) or [query.strip()]


def _keywords(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]*|[\u4e00-\u9fff]{2,}", text)
    ignored = {"请问", "帮我", "告诉我", "多少", "是什么", "详细", "内容", "文档"}
    return list(dict.fromkeys(word for word in words if word not in ignored))[:12]


class RuleClassificationStrategy:
    """Conservative fallback; it may classify but never invent a comparison edge."""

    def classify_sync(self, *, query: str | list[str]) -> QueryClassificationOutput:
        focused: list[RoutedQueryGroupDraft] = []
        broad: list[RoutedQueryGroupDraft] = []
        direct: list[RoutedQueryGroupDraft] = []
        scope: list[ScopeDirectGroupDraft] = []
        for part in _split_query(query_as_text(query)):
            if any(marker in part for marker in _BROAD_PATTERNS):
                target = broad
            elif _is_scope_query(part):
                scope.append(ScopeDirectGroupDraft(queries=[part]))
                continue
            else:
                target = direct if _is_direct_query(part) else focused
            target.append(
                RoutedQueryGroupDraft(
                    queries=[part],
                    target_docs_description=part,
                    target_docs_keywords=_keywords(part),
                )
            )
        return QueryClassificationOutput(
            routed_focused=focused,
            routed_broad=broad,
            routed_direct=direct,
            scope_direct=scope,
        )

    async def classify(
        self, *, request_id: str, query: str | list[str], scope_document_count: int = 0
    ) -> QueryClassificationOutput:
        del request_id, scope_document_count
        return self.classify_sync(query=query)


@dataclass(frozen=True, slots=True)
class ClassificationRun:
    groups: list[QueryGroup]
    warnings: list[RetrievalWarning]
    degraded: bool = False
    llm_result: LLMResult | None = None
    classification_trace: tuple[dict[str, Any], ...] = ()


class ClassificationService:
    def __init__(
        self, *, primary: ClassificationStrategy, fallback: RuleClassificationStrategy
    ) -> None:
        self.primary = primary
        self.fallback = fallback

    async def classify(
        self,
        *,
        request_id: str,
        query: str | list[str],
        scope_document_count: int = 0,
        debug_enabled: bool = False,
    ) -> ClassificationRun:
        del debug_enabled
        query_text = query_as_text(query)
        try:
            output = await self.primary.classify(
                request_id=request_id,
                query=query,
                scope_document_count=scope_document_count,
            )
            _validate_classification_rewrite(query=query_text, output=output)
            groups = _correct_unambiguous_categories(materialize_groups(output))
            return ClassificationRun(groups=groups, warnings=[])
        except Exception:
            output = await self.fallback.classify(
                request_id=request_id,
                query=query,
                scope_document_count=scope_document_count,
            )
            groups = materialize_groups(output)
            groups = [group.model_copy(update={"degraded": True}) for group in groups]
            warning = RetrievalWarning(
                code="CLASSIFICATION_RULE_FALLBACK",
                message="classification model failed; conservative rule classification was used",
                affected_group_refs=[group.group_ref for group in groups],
                retryable=True,
            )
            return ClassificationRun(groups=groups, warnings=[warning], degraded=True)


def _correct_unambiguous_categories(groups: list[QueryGroup]) -> list[QueryGroup]:
    corrected: list[QueryGroup] = []
    for group in groups:
        categories = {
            category
            for query in group.queries
            if (category := _unambiguous_category(query)) is not None
        }
        category = next(iter(categories)) if len(categories) == 1 else group.category
        corrected.append(group.model_copy(update={"category": category}))
    return corrected


def _validate_classification_rewrite(*, query: str, output: QueryClassificationOutput) -> None:
    original_terms = set(tokenize_query(query))
    seen_queries: set[str] = set()
    total = 0
    for category in (
        "routed_focused",
        "routed_broad",
        "routed_direct",
        "scope_direct",
    ):
        for draft in getattr(output, category):
            for rewritten in draft.queries:
                normalized = re.sub(r"\s+", "", rewritten.casefold())
                if len(normalized) < 2:
                    raise ValueError("classification produced an empty or trivial question")
                if normalized in seen_queries:
                    raise ValueError("classification repeated a question across groups")
                seen_queries.add(normalized)
                total += 1
                if not (set(tokenize_query(rewritten)) & original_terms):
                    raise ValueError("classification introduced a question unrelated to the query")
    if total == 0:
        raise ValueError("classification produced no questions")
