from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from app.budgeting.lpt import BudgetItem, group_by_token
from app.budgeting.tokens import (
    QueryBudgetError,
    ensure_fixed_prompt_fits,
    split_text_by_token,
)
from app.core.models import CandidateChunk
from app.db.repositories import PageRecord
from app.llm.gateway import LLMGateway, LLMRequest
from app.workflows.classification.models import QueryGroup
from app.workflows.focused_search.models import (
    PageDecision,
    PageDecisionBatch,
    PageInspectionResult,
)
from app.workflows.keyword_ranking import score_text, tokenize_query

PAGE_INSPECTION_PROMPT = """# 角色与任务
你是单篇文档的页面证据判定器。你只判断页面与问题组的相关性，不回答用户问题，不改写页面内容。

# 输入说明
用户消息包含同一问题组中的全部独立问题，以及一批带 page_number 的页面原文。组内问题没有主次。

# 决策标准
- accept：页面直接包含回答任一组内问题所需的事实、定义、步骤、数值或关键上下文。
- possible：页面可能提供必要背景、上下文不完整，或与问题相关但不能单独形成直接证据。
- 不输出的页面：代码统一视为 reject。
- 一个页面命中组内任一问题即可保留；不得因它没有同时回答全部问题而拒绝。

# 禁止事项
- 不得输出摘录、理由、答案或总结。
- 不得新增、改写或重复输入中的 page_number。
- 只挑出 accept 和 possible 页面；不得显式输出 reject，无关页面直接省略，全部无关时返回空列表。
- 不得仅因页面是目录、封面或前言就机械接受；必须依据其对当前问题的证据价值。

# 示例
- 查询营业额时，含营业收入表格或明确数值的页面为 accept。
- 只提到公司背景、没有相关经营指标的页面通常为 reject。
- 流程跨页且当前页仅包含相关步骤标题时可以为 possible。

# 输出约束
严格遵守结构化输出契约，只返回应标为 accept 或 possible 的 page_number。
未返回的页面由代码确定为 reject。
"""


class PageRepository(Protocol):
    def fetch_pages(
        self, *, user_id: str, kb_id: str, doc_id: str, pages: list[int] | None = None
    ) -> list[PageRecord]: ...


T = TypeVar("T")


class AsyncRepositoryExecutor(Protocol):
    def run(self, function: Any, /, **kwargs: Any) -> Awaitable[Any]: ...


@dataclass(frozen=True, slots=True)
class _InspectionBatch:
    records: tuple[PageRecord, ...]


def page_decision_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "page_decisions",
            "strict": True,
            "schema": PageDecisionBatch.model_json_schema(),
        },
    }


class PageInspector:
    def __init__(
        self,
        *,
        repository: PageRepository,
        gateway: LLMGateway,
        model: str,
        context_window: int,
        output_tokens: int,
        safety_margin: int,
        count_tokens: Callable[[str], int],
        accept_score: float = 20.0,
        possible_score: float = 1.0,
        db_executor: AsyncRepositoryExecutor | None = None,
        session_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.gateway = gateway
        self.model = model
        self.context_window = context_window
        self.output_tokens = output_tokens
        self.safety_margin = safety_margin
        self.count_tokens = count_tokens
        self.accept_score = accept_score
        self.possible_score = possible_score
        self.db_executor = db_executor
        self.session_id = session_id

    async def inspect(
        self,
        *,
        request_id: str,
        user_id: str,
        kb_id: str,
        group: QueryGroup,
        doc_id: str,
        pages: list[int],
    ) -> PageInspectionResult:
        requested = sorted({int(page) for page in pages if int(page) > 0})
        fetch_kwargs = {
            "user_id": user_id,
            "kb_id": kb_id,
            "doc_id": doc_id,
            "pages": requested,
            "session_id": self.session_id,
        }
        if self.db_executor is None:
            records = self.repository.fetch_pages(**fetch_kwargs)
        else:
            records = await self.db_executor.run(self.repository.fetch_pages, **fetch_kwargs)
        by_page = {record.page_number: record for record in records}
        if set(by_page) != set(requested):
            missing = sorted(set(requested) - set(by_page))
            raise ValueError(f"requested pages do not exist in scoped document: {missing}")
        fixed_payload = {"group_ref": group.group_ref, "queries": group.queries}
        fixed_text = PAGE_INSPECTION_PROMPT + json.dumps(fixed_payload, ensure_ascii=False)
        fixed_used = ensure_fixed_prompt_fits(
            fixed_text=fixed_text,
            context_window=self.context_window,
            safety_margin=self.safety_margin + self.output_tokens,
            count_tokens=self.count_tokens,
        )
        item_budget = self.context_window - fixed_used
        normal_items: list[BudgetItem] = []
        oversized_batches: list[_InspectionBatch] = []
        for record in records:
            serialized = json.dumps(
                {"page_number": record.page_number, "content": record.content},
                ensure_ascii=False,
            )
            if self.count_tokens(serialized) <= item_budget:
                normal_items.append(BudgetItem(item_id=str(record.page_number), text=serialized))
                continue
            wrapper_cost = self.count_tokens(
                json.dumps(
                    {"page_number": record.page_number, "content": ""},
                    ensure_ascii=False,
                )
            )
            windows = split_text_by_token(
                record.content,
                max_tokens=max(1, item_budget - wrapper_cost),
                count_tokens=self.count_tokens,
                overlap_ratio=0.1,
            )
            oversized_batches.extend(
                _InspectionBatch(records=(record.model_copy(update={"content": window}),))
                for window in windows
            )
        try:
            token_groups = group_by_token(
                normal_items,
                context_window=self.context_window,
                reserved_tokens=fixed_used,
                count_tokens=self.count_tokens,
                target_load_ratio=0.65,
            )
        except ValueError as exc:
            raise QueryBudgetError(str(exc)) from exc
        batches = [
            _InspectionBatch(records=tuple(by_page[int(item.item_id)] for item in batch.items))
            for batch in token_groups
        ] + oversized_batches

        results = await asyncio.gather(
            *[
                self._inspect_batch(
                    request_id=request_id,
                    group=group,
                    records=list(batch.records),
                    index=index,
                    count=len(batches),
                )
                for index, batch in enumerate(batches)
            ],
            return_exceptions=True,
        )
        grades_by_page: dict[int, list[str]] = {page: [] for page in requested}
        llm_decided: set[int] = set()
        fallback_pages: set[int] = set()
        for batch, result in zip(batches, results, strict=True):
            expected = {record.page_number for record in batch.records}
            if isinstance(result, Exception):
                for record in batch.records:
                    grades_by_page[record.page_number].append(
                        self._rule_decision(group=group, page=record).grade
                    )
                    fallback_pages.add(record.page_number)
                continue
            output = result
            returned_pages = [decision.page_number for decision in output.decisions]
            returned = set(returned_pages)
            if len(returned_pages) != len(returned) or not returned.issubset(expected):
                for record in batch.records:
                    grades_by_page[record.page_number].append(
                        self._rule_decision(group=group, page=record).grade
                    )
                    fallback_pages.add(record.page_number)
                continue
            for decision in output.decisions:
                grades_by_page[decision.page_number].append(decision.grade)
            for omitted_page in expected - returned:
                grades_by_page[omitted_page].append("reject")
            llm_decided.update(expected)
        grade_order = {"accept": 0, "possible": 1, "reject": 2}
        decision_map = {
            page: PageDecision(
                page_number=page,
                grade=min(grades or ["reject"], key=lambda grade: grade_order[grade]),
            )
            for page, grades in grades_by_page.items()
        }
        decisions = [decision_map[page] for page in requested]
        chunks = [
            CandidateChunk(
                chunk_id=f"{record.doc_id}:page:{record.page_number}",
                document_id=record.doc_id,
                document_ids=[record.doc_id],
                document_name=record.document_name,
                page_number=record.page_number,
                path=f"document:{record.doc_id}:page:{record.page_number}",
                content=record.content,
                source_type="page",
                category="routed_focused",
                group_matches={group.group_ref: decision_map[record.page_number].grade},
                questions_by_group={group.group_ref: group.queries},
                rule_score=(
                    self._rule_score(group=group, page=record)
                    if record.page_number in fallback_pages
                    else None
                ),
            )
            for record in records
            if decision_map[record.page_number].grade != "reject"
        ]
        source = "llm" if not fallback_pages else ("mixed" if llm_decided else "rule_fallback")
        return PageInspectionResult(
            decisions=decisions,
            evaluated_pages=requested,
            decision_source=source,
            chunks=sorted(chunks, key=lambda chunk: int(chunk.page_number or 0)),
            llm_request_count=len(batches),
        )

    async def _inspect_batch(
        self,
        *,
        request_id: str,
        group: QueryGroup,
        records: list[PageRecord],
        index: int,
        count: int,
    ) -> PageDecisionBatch:
        content = json.dumps(
            {
                "group_ref": group.group_ref,
                "queries": group.queries,
                "pages": [
                    {"page_number": record.page_number, "content": record.content}
                    for record in records
                ],
            },
            ensure_ascii=False,
        )
        result = await self.gateway.complete_json(
            LLMRequest(
                request_id=request_id,
                phase=f"page_inspection:{index + 1}/{count}",
                messages=[
                    {"role": "system", "content": PAGE_INSPECTION_PROMPT},
                    {"role": "user", "content": content},
                ],
                model=self.model,
                max_tokens=self.output_tokens,
                response_format=page_decision_schema(),
            )
        )
        return PageDecisionBatch.model_validate(result.data)

    def _rule_score(self, *, group: QueryGroup, page: PageRecord) -> float:
        query = " ".join(group.queries)
        return score_text(
            query,
            tokenize_query(query),
            content=page.content,
            metadata=page.document_name,
        )

    def _rule_decision(self, *, group: QueryGroup, page: PageRecord) -> PageDecision:
        score = self._rule_score(group=group, page=page)
        grade = (
            "accept"
            if score >= self.accept_score
            else ("possible" if score >= self.possible_score else "reject")
        )
        return PageDecision(page_number=page.page_number, grade=grade)
