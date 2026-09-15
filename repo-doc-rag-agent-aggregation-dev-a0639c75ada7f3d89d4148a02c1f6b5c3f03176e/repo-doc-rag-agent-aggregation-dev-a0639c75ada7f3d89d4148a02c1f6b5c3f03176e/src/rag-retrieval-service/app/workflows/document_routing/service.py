from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.api.schemas import RetrievalWarning
from app.budgeting.lpt import BudgetItem, group_by_token
from app.budgeting.tokens import QueryBudgetError, ensure_fixed_prompt_fits, split_text_by_token
from app.db.repositories import DocumentProfile
from app.llm.gateway import LLMGateway, LLMRequest
from app.workflows.classification.models import QueryGroup
from app.workflows.document_routing.models import (
    DocumentRoute,
    DocumentRouteBatchOutput,
    PrefilterResult,
    RoutedDocument,
)
from app.workflows.document_routing.prefilter import (
    _uses_scope_relative_reference,
    _uses_whole_scope_reference,
    keyword_prefilter,
)

ROUTING_SYSTEM_PROMPT = """# 角色与任务
你是多文档路由器。你只根据文档名、文档类型和数据库已有摘要，判断每篇候选文档是否属于每个问题组；你不检索正文，也不回答问题。

# 输入说明
用户消息包含问题组、候选文档元信息和 required_pairs。
required_pairs 明确列出本批次允许选择的 group_ref 与 doc_id 组合。

# 决策标准
- accept：文档名或摘要明确表明该文档就是问题组需要的目标文档。
- possible：存在合理相关性，但元信息不足以确认。
- 不输出的组合：代码统一视为 reject。
- 判断目标文档身份，而不是判断摘要里是否已经包含问题答案。
- 同一文档可以被多个问题组接受；每个组合必须独立判断。

# 禁止事项
- 不得阅读或推断输入之外的正文，不得用常识补全缺失信息。
- 只能引用 required_pairs 中已有的 group_ref 和 doc_id，不得新增或重复组合。
- 只挑出 accept 和 possible；不得显式输出 reject，不相关组合直接省略，全部不相关时返回空列表。
- 不得输出理由、答案、新 ID 或自然语言说明。

# 示例
- 问题组目标是“A 公司年度报告”，文档名为“A 公司 2025 年年度报告”时通常为 accept。
- 只有“行业研究资料”且摘要未指向 A 公司时通常为 reject。
- 文档名模糊但摘要提及 A 公司经营情况时可以为 possible。

# 输出约束
严格遵守结构化输出契约，只返回 required_pairs 中应标为 accept 或 possible 的组合。
未返回的组合由代码确定为 reject。
"""

def route_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "document_route_batch",
            "strict": True,
            "schema": DocumentRouteBatchOutput.model_json_schema(),
        },
    }


@dataclass(frozen=True, slots=True)
class _Batch:
    documents: list[dict[str, Any]]
    pairs: list[tuple[str, str]]


class DocumentRoutingService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        model: str,
        context_window: int,
        output_tokens: int,
        safety_margin: int,
        count_tokens: Callable[[str], int],
        prefilter_threshold: float = 0.05,
    ) -> None:
        self.gateway = gateway
        self.model = model
        self.context_window = context_window
        self.output_tokens = output_tokens
        self.safety_margin = safety_margin
        self.count_tokens = count_tokens
        self.prefilter_threshold = prefilter_threshold

    async def route(
        self,
        *,
        request_id: str,
        groups: list[QueryGroup],
        documents: list[DocumentProfile],
        prefiltered: dict[str, PrefilterResult] | None = None,
    ) -> tuple[list[DocumentRoute], list[RetrievalWarning]]:
        if not groups:
            return [], []
        prefiltered = prefiltered or keyword_prefilter(
            groups=groups, documents=documents, threshold=self.prefilter_threshold
        )
        fixed_payload = {
            "groups": [
                {
                    "group_ref": group.group_ref,
                    "queries": group.queries,
                    "target_docs_description": group.target_docs_description,
                    "target_docs_keywords": group.target_docs_keywords,
                }
                for group in groups
            ]
        }
        fixed_text = ROUTING_SYSTEM_PROMPT + json.dumps(fixed_payload, ensure_ascii=False)
        fixed_used = ensure_fixed_prompt_fits(
            fixed_text=fixed_text,
            context_window=self.context_window,
            safety_margin=self.safety_margin + self.output_tokens,
            count_tokens=self.count_tokens,
        )
        warnings: list[RetrievalWarning] = []
        unique_docs: dict[str, DocumentProfile] = {}
        for result in prefiltered.values():
            for candidate in result.candidates:
                unique_docs[candidate.document.doc_id] = candidate.document
        item_budget = self.context_window - fixed_used
        routing_views: dict[str, dict[str, Any]] = {}
        truncated_doc_ids: list[str] = []
        for document in unique_docs.values():
            view = {
                "doc_id": document.doc_id,
                "doc_name": document.doc_name,
                "doc_type": document.doc_type,
                "doc_description": document.doc_description,
            }
            serialized = json.dumps(view, ensure_ascii=False)
            if self.count_tokens(serialized) > item_budget:
                empty_view = {**view, "doc_description": ""}
                prefix_budget = item_budget - self.count_tokens(
                    json.dumps(empty_view, ensure_ascii=False)
                )
                if prefix_budget <= 0:
                    raise QueryBudgetError(
                        f"document routing metadata exceeds context budget: {document.doc_id}"
                    )
                view["doc_description"] = split_text_by_token(
                    document.doc_description,
                    max_tokens=prefix_budget,
                    count_tokens=self.count_tokens,
                    overlap_ratio=0,
                )[0]
                truncated_doc_ids.append(document.doc_id)
                serialized = json.dumps(view, ensure_ascii=False)
            routing_views[document.doc_id] = view
        if truncated_doc_ids:
            warnings.append(
                RetrievalWarning(
                    code="DOCUMENT_DESCRIPTION_TRUNCATED_FOR_ROUTING",
                    message="oversized document descriptions used a fixed token prefix for routing",
                    affected_document_ids=sorted(truncated_doc_ids),
                )
            )
        items = [
            BudgetItem(
                item_id=doc_id,
                text=json.dumps(view, ensure_ascii=False),
            )
            for doc_id, view in routing_views.items()
        ]
        try:
            token_groups = group_by_token(
                items,
                context_window=self.context_window,
                reserved_tokens=fixed_used,
                count_tokens=self.count_tokens,
                target_load_ratio=0.65,
            )
        except ValueError as exc:
            raise QueryBudgetError(str(exc)) from exc
        batches: list[_Batch] = []
        for token_group in token_groups:
            batch_docs = [routing_views[item.item_id] for item in token_group.items]
            batch_doc_ids = {str(document["doc_id"]) for document in batch_docs}
            pairs = [
                (group_ref, candidate.document.doc_id)
                for group_ref, result in prefiltered.items()
                for candidate in result.candidates
                if candidate.document.doc_id in batch_doc_ids
            ]
            batches.append(_Batch(documents=batch_docs, pairs=pairs))

        results = await asyncio.gather(
            *[
                self._route_batch(
                    request_id=request_id,
                    batch=batch,
                    fixed_payload=fixed_payload,
                    index=index,
                    count=len(batches),
                )
                for index, batch in enumerate(batches)
            ],
            return_exceptions=True,
        )
        decisions: dict[tuple[str, str], tuple[str, str]] = {}
        failed_pairs: set[tuple[str, str]] = set()
        for batch, result in zip(batches, results, strict=True):
            if isinstance(result, Exception):
                failed_pairs.update(batch.pairs)
                continue
            expected = set(batch.pairs)
            returned_pairs = [
                (decision.group_ref, decision.doc_id) for decision in result.decisions
            ]
            returned = set(returned_pairs)
            if len(returned_pairs) != len(returned) or not returned.issubset(expected):
                failed_pairs.update(expected)
                continue
            for pair in expected:
                decisions[pair] = ("reject", "llm")
            for decision in result.decisions:
                pair = (decision.group_ref, decision.doc_id)
                decisions[pair] = (decision.grade, "llm")
        if failed_pairs:
            for pair in failed_pairs:
                result = prefiltered[pair[0]]
                candidate = next(
                    item for item in result.candidates if item.document.doc_id == pair[1]
                )
                grade = "accept" if candidate.keyword_score >= 0.65 else "possible"
                decisions[pair] = (grade, "rule_fallback")
            warnings.append(
                RetrievalWarning(
                    code="DOCUMENT_ROUTING_BATCH_RULE_FALLBACK",
                    message="one or more document-routing batches used deterministic rules",
                    affected_group_refs=sorted({pair[0] for pair in failed_pairs}),
                    affected_document_ids=sorted({pair[1] for pair in failed_pairs}),
                    retryable=True,
                )
            )

        routes: list[DocumentRoute] = []
        for group_ref, result in prefiltered.items():
            group = next(group for group in groups if group.group_ref == group_ref)
            accept: list[RoutedDocument] = []
            possible: list[RoutedDocument] = []
            rejected_by_llm = 0
            for candidate in result.candidates:
                grade, source = decisions[(group_ref, candidate.document.doc_id)]
                if _uses_whole_scope_reference(group) or (
                    len(documents) == 1 and _uses_scope_relative_reference(group)
                ):
                    grade, source = "accept", "rule_fallback"
                if grade == "reject":
                    rejected_by_llm += 1
                    continue
                item = RoutedDocument(
                    document=candidate.document,
                    grade=grade,
                    keyword_score=candidate.keyword_score,
                    decision_source=source,
                )
                (accept if grade == "accept" else possible).append(item)

            def route_key(item: RoutedDocument) -> tuple[float, str, str]:
                return (-item.keyword_score, item.document.doc_name, item.document.doc_id)

            routes.append(
                DocumentRoute(
                    group_ref=group_ref,
                    accept_docs=sorted(accept, key=route_key),
                    possible_docs=sorted(possible, key=route_key),
                    rejected_document_count=result.rejected_count + rejected_by_llm,
                    prefilter_candidate_count=len(result.candidates),
                    prefilter_rejected_count=result.rejected_count,
                    degraded=any(pair[0] == group_ref for pair in failed_pairs),
                )
            )
        return routes, warnings

    async def _route_batch(
        self,
        *,
        request_id: str,
        batch: _Batch,
        fixed_payload: dict[str, Any],
        index: int,
        count: int,
    ) -> DocumentRouteBatchOutput:
        content = json.dumps(
            {
                **fixed_payload,
                "documents": batch.documents,
                "required_pairs": batch.pairs,
            },
            ensure_ascii=False,
        )
        response_format = route_schema()
        request = LLMRequest(
            request_id=request_id,
            phase=f"document_routing:{index + 1}/{count}",
            messages=[
                {"role": "system", "content": ROUTING_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            model=self.model,
            max_tokens=self.output_tokens,
            response_format=response_format,
        )
        try:
            result = await self.gateway.complete_json(request)
            return DocumentRouteBatchOutput.model_validate(result.data)
        except ValidationError as exc:
            raise ValueError("invalid document routing response") from exc
