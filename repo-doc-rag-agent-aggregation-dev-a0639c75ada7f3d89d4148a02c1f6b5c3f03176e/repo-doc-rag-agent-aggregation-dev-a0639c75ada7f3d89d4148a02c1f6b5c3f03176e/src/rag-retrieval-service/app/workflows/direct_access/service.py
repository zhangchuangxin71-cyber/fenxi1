from __future__ import annotations

from typing import Any, Protocol

from app.api.schemas import RetrievalWarning
from app.core.models import CandidateChunk, ToolOutput
from app.db.repositories import DocumentProfile, NodeRecord
from app.workflows.classification.models import QueryGroup
from app.workflows.direct_access.models import DirectPlanError, DirectToolCall
from app.workflows.direct_access.planner import LLMDirectPlanner, RuleDirectPlanner


class DirectExecutor(Protocol):
    def get_docs_metainfo(self, **kwargs: object) -> ToolOutput: ...
    def get_docs_description(self, **kwargs: object) -> ToolOutput: ...
    def get_content_of_pages(self, **kwargs: object) -> ToolOutput: ...
    def get_content_of_chapters(self, **kwargs: object) -> ToolOutput: ...
    def view_doc_title_tree(self, **kwargs: object) -> ToolOutput: ...


class DirectAccessService:
    def __init__(
        self,
        *,
        primary: LLMDirectPlanner,
        fallback: RuleDirectPlanner,
        executor: DirectExecutor,
        db_executor: Any | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.executor = executor
        self.db_executor = db_executor

    async def run(
        self, *, request_id: str, group: QueryGroup, documents: list[DocumentProfile]
    ) -> ToolOutput:
        query = "；".join(group.queries)
        warnings: list[RetrievalWarning] = []
        try:
            plan = await self.primary.plan(
                request_id=request_id,
                group_ref=group.group_ref,
                query=query,
                documents=documents,
            )
        except Exception:
            try:
                plan = self.fallback.plan(query=query, documents=documents)
                warnings.append(
                    RetrievalWarning(
                        code="DIRECT_TOOL_PLANNER_RULE_FALLBACK",
                        message="direct tool planning used deterministic rules",
                        affected_group_refs=[group.group_ref],
                        retryable=True,
                    )
                )
            except DirectPlanError:
                return ToolOutput(
                    chunks=[],
                    warnings=[
                        RetrievalWarning(
                            code="DIRECT_TOOL_PLANNER_FAILED",
                            message=(
                                "direct resource request could not be resolved to a safe tool call"
                            ),
                            affected_group_refs=[group.group_ref],
                            retryable=True,
                        )
                    ],
                    coverage_complete=False,
                )
        chunks: list[CandidateChunk] = []
        inspected_node_count = 0
        inspected_page_count = 0
        for call in plan.calls:
            output = await self._execute(call=call, group=group, profiles=documents)
            chunks.extend(output.chunks)
            warnings.extend(output.warnings)
            inspected_node_count += output.inspected_node_count
            inspected_page_count += output.inspected_page_count
        return ToolOutput(
            chunks=chunks,
            warnings=warnings,
            coverage_complete=bool(chunks),
            inspected_node_count=inspected_node_count,
            inspected_page_count=inspected_page_count,
        )

    async def _execute(
        self, *, call: DirectToolCall, group: QueryGroup, profiles: list[DocumentProfile]
    ) -> ToolOutput:
        common = {"group_ref": group.group_ref, "questions": group.queries}
        if call.tool_name == "get_docs_metainfo":
            return await self._call(
                self.executor.get_docs_metainfo,
                **common,
                profiles=profiles,
                doc_ids=call.doc_ids,
                fields=call.metainfo_fields,
            )
        if call.tool_name == "get_docs_description":
            return await self._call(
                self.executor.get_docs_description,
                **common,
                profiles=profiles,
                doc_ids=call.doc_ids,
            )
        if call.tool_name == "get_content_of_pages":
            return await self._call(
                self.executor.get_content_of_pages, **common, pages_by_doc=call.page_numbers_by_doc
            )
        if call.tool_name == "get_content_of_chapters":
            node_ids = dict(call.node_ids_by_doc)
            resolved_node_count = 0
            for doc_id, chapter_numbers in call.chapter_numbers_by_doc.items():
                nodes = await self._call(
                    self.executor.repository.fetch_nodes,
                    user_id=self.executor.user_id,
                    kb_id=self.executor.kb_id,
                    doc_id=doc_id,
                    session_id=self.executor.session_id,
                )
                resolved_node_count += len(nodes)
                ordered = sorted(
                    [node for node in nodes if node.parent_node_id is not None],
                    key=lambda node: (node.level or 999, node.sibling_order, node.node_id),
                )
                selected: list[str] = []
                for chapter_number in chapter_numbers:
                    match = _chapter_node(ordered, chapter_number)
                    if match is not None:
                        selected.append(match.node_id)
                node_ids[doc_id] = selected
            output = await self._call(
                self.executor.get_content_of_chapters, **common, node_ids_by_doc=node_ids
            )
            return output.model_copy(
                update={"inspected_node_count": (output.inspected_node_count + resolved_node_count)}
            )
        return await self._call(
            self.executor.view_doc_title_tree,
            **common,
            profiles=profiles,
            doc_ids=call.doc_ids,
            level=call.tree_level,
            include_node_id=call.include_node_id,
        )

    async def _call(self, function: Any, **kwargs: Any) -> Any:
        if self.db_executor is None:
            return function(**kwargs)
        return await self.db_executor.run(function, **kwargs)


def _chapter_node(nodes: list[NodeRecord], chapter_number: int) -> NodeRecord | None:
    marker = f"第{chapter_number}章"
    exact = next((node for node in nodes if marker in node.title.replace(" ", "")), None)
    if exact is not None:
        return exact
    top_level = min((node.level for node in nodes if node.level is not None), default=None)
    candidates = [node for node in nodes if node.level == top_level]
    index = chapter_number - 1
    return candidates[index] if 0 <= index < len(candidates) else None
