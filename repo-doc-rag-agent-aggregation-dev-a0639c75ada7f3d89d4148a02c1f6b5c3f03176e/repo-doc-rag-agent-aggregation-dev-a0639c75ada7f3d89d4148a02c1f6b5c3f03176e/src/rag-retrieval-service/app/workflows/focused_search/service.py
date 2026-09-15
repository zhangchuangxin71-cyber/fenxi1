from __future__ import annotations

import json
from typing import Any, Protocol

from app.api.schemas import RetrievalWarning
from app.db.repositories import NodeRecord
from app.llm.gateway import LLMGateway, LLMRequest, LLMToolCall
from app.llm.tools import strict_function_tool
from app.tools.node_tree import NodeTreeScanner, NodeTreeScanResult, NodeView
from app.workflows.classification.models import QueryGroup
from app.workflows.document_routing.models import RoutedDocument
from app.workflows.focused_search.models import FocusedSearchResult
from app.workflows.focused_search.page_inspector import AsyncRepositoryExecutor, PageInspector

TREE_NAVIGATION_PROMPT = """# 角色与任务
你是单篇文档的目录树检索规划器。你只通过目录树缩小候选页面范围，不回答用户问题。

# 输入说明
问题组中的所有独立问题同等重要，必须共同决定检索范围。
scan_node_tree 的结果包含 node_id、标题、摘要和对应页码范围；这些 node 信息只用于导航，
不是最终证据。

# 工具使用规则
- 当前节点范围仍然过大且标题树清晰时，对一个或多个相关 node_id 调用 scan_node_tree。
- 当前候选节点范围已经足够小，或继续向下扫描不会增加价值时，
  调用 finish_tree_navigation 并选择 finished。
- 标题树混乱、标题与摘要无法支持可靠导航时，
  调用 finish_tree_navigation 并选择 fallback_full_document。
- 多个不相邻章节都可能包含证据时，必须并行扫描全部相关节点，不能只保留其中一个。

# 禁止事项
- 只能引用工具结果中已经出现的 node_id，不得编造节点。
- 不得维护、计算或输出 page 列表；页码展开由代码完成。
- 不得输出答案、页原文、摘要、理由或自然语言工具说明。

# 示例
- 一级标题中“财务数据”和“经营回顾”都可能回答问题时，同时对两个节点调用 scan_node_tree。
- 当前候选节点已经是无子节点的叶子章节时，调用 finish_tree_navigation(finished)。
- 目录只有乱码或层级明显错误时，调用 finish_tree_navigation(fallback_full_document)。

# 输出约束
每轮只调用 scan_node_tree，或者只调用一次 finish_tree_navigation；不得混合两类工具。
"""

TREE_NAVIGATION_TOOLS = [
    strict_function_tool(
        name="scan_node_tree",
        description="扫描当前候选目录节点下指定层级的子节点，以进一步缩小候选页面范围。",
        properties={
            "root_node_id": {
                "type": "string",
                "description": "要继续向下扫描的当前候选节点 ID。",
            },
            "level": {
                "type": "integer",
                "minimum": 1,
                "maximum": 4,
                "description": "从父节点向下扫描的层数，通常使用 1。",
            },
        },
        required=["root_node_id", "level"],
    ),
    strict_function_tool(
        name="finish_tree_navigation",
        description="结束目录树导航，由代码把当前候选节点展开为候选页面。",
        properties={
            "decision": {
                "type": "string",
                "enum": ["finished", "fallback_full_document"],
                "description": "使用当前候选范围结束，或因目录树不可用而遍历全文。",
            }
        },
        required=["decision"],
    ),
]


class FocusedRepository(Protocol):
    def fetch_nodes(self, *, user_id: str, kb_id: str, doc_id: str) -> list[NodeRecord]: ...


class FocusedSearchService:
    def __init__(
        self,
        *,
        repository: FocusedRepository,
        scanner: NodeTreeScanner,
        page_inspector: PageInspector,
        gateway: LLMGateway,
        model: str,
        max_tree_steps: int,
        tree_output_tokens: int,
        db_executor: AsyncRepositoryExecutor | None = None,
        session_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.scanner = scanner
        self.page_inspector = page_inspector
        self.gateway = gateway
        self.model = model
        self.max_tree_steps = max(1, int(max_tree_steps))
        self.tree_output_tokens = max(1, int(tree_output_tokens))
        self.db_executor = db_executor
        self.session_id = session_id

    async def search(
        self,
        *,
        request_id: str,
        user_id: str,
        kb_id: str,
        group: QueryGroup,
        routed_document: RoutedDocument,
        trace_collector: Any | None = None,
    ) -> FocusedSearchResult:
        doc = routed_document.document
        records = await self._fetch_nodes(user_id=user_id, kb_id=kb_id, doc_id=doc.doc_id)
        roots = [node for node in records if node.parent_node_id is None]
        roots.sort(key=lambda node: (node.sibling_order, node.node_id))
        warnings: list[RetrievalWarning] = []
        initial: list[NodeTreeScanResult] = []
        for root in roots:
            initial.append(await self._scan(doc_id=doc.doc_id, root_node_id=root.node_id, level=1))
        self._trace(
            trace_collector,
            group_ref=group.group_ref,
            document_id=doc.doc_id,
            kind="tool_result",
            payload={
                "tool": "scan_node_tree",
                "stage": "initial",
                "results": [result.model_dump(mode="json") for result in initial],
            },
        )
        frontier = [node for result in initial if result.status == "ok" for node in result.nodes]
        inspected_node_ids = {node.node_id for node in frontier}
        tree_usable = bool(frontier) and all(result.status == "ok" for result in initial)
        pages: list[int]
        tree_calls = 0
        degraded = False
        if not tree_usable:
            pages = self._full_pages(doc.page_count, records)
        else:
            pages = []
            known: dict[str, NodeView] = {node.node_id: node for node in frontier}
            messages = self._initial_messages(
                group=group,
                document_id=doc.doc_id,
                roots=roots,
                scans=initial,
            )
            for step in range(self.max_tree_steps):
                try:
                    tree_calls += 1
                    tool_calls = await self._call_agent(
                        request_id=request_id,
                        document_id=doc.doc_id,
                        messages=messages,
                        step=step,
                    )
                    self._trace(
                        trace_collector,
                        group_ref=group.group_ref,
                        document_id=doc.doc_id,
                        kind="tool_call",
                        payload={
                            "step": step + 1,
                            "calls": [
                                {"name": call.name, "arguments": call.arguments}
                                for call in tool_calls
                            ],
                        },
                    )
                    messages.append(_assistant_tool_call_message(tool_calls))
                    names = {call.name for call in tool_calls}
                    if names == {"finish_tree_navigation"}:
                        if len(tool_calls) != 1:
                            raise ValueError("tree navigation returned multiple finish calls")
                        decision = tool_calls[0].arguments["decision"]
                        if decision == "fallback_full_document":
                            pages = self._full_pages(doc.page_count, records)
                        elif decision == "finished":
                            pages = _pages_for_nodes(
                                [node.node_id for node in frontier], known
                            )
                        else:
                            raise ValueError("tree navigation returned an invalid decision")
                        break
                    if names != {"scan_node_tree"}:
                        raise ValueError("tree navigation mixed incompatible tool calls")
                    next_frontier: list[NodeView] = []
                    for call in tool_calls:
                        node_id = str(call.arguments["root_node_id"])
                        level = int(call.arguments["level"])
                        if node_id not in known:
                            raise ValueError("tree navigation referenced an unknown node")
                        scan = await self._scan(
                            doc_id=doc.doc_id, root_node_id=node_id, level=level
                        )
                        self._trace(
                            trace_collector,
                            group_ref=group.group_ref,
                            document_id=doc.doc_id,
                            kind="tool_result",
                            payload={
                                "tool": "scan_node_tree",
                                "step": step + 1,
                                "result": scan.model_dump(mode="json"),
                            },
                        )
                        messages.append(_tool_result_message(call=call, result=scan))
                        inspected_node_ids.add(node_id)
                        if scan.status == "ok" and scan.nodes:
                            next_frontier.extend(scan.nodes)
                            for node in scan.nodes:
                                known[node.node_id] = node
                                inspected_node_ids.add(node.node_id)
                        else:
                            next_frontier.append(known[node_id])
                    frontier = _unique_nodes(next_frontier)
                except Exception as exc:
                    pages = self._full_pages(doc.page_count, records)
                    self._trace(
                        trace_collector,
                        group_ref=group.group_ref,
                        document_id=doc.doc_id,
                        kind="fallback",
                        payload={
                            "code": "TREE_NAVIGATION_RULE_FALLBACK",
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                            "fallback_page_count": len(pages),
                        },
                    )
                    warnings.append(
                        RetrievalWarning(
                            code="TREE_NAVIGATION_RULE_FALLBACK",
                            message=(
                                "tree navigation failed; the full document was sent to page "
                                "inspection"
                            ),
                            affected_group_refs=[group.group_ref],
                            affected_document_ids=[doc.doc_id],
                            retryable=True,
                        )
                    )
                    degraded = True
                    break
            if not pages:
                pages = _pages_for_nodes([node.node_id for node in frontier], known)
        if not pages:
            pages = self._full_pages(doc.page_count, records)
        inspection = await self.page_inspector.inspect(
            request_id=request_id,
            user_id=user_id,
            kb_id=kb_id,
            group=group,
            doc_id=doc.doc_id,
            pages=pages,
        )
        self._trace(
            trace_collector,
            group_ref=group.group_ref,
            document_id=doc.doc_id,
            kind="tool_result",
            payload={
                "tool": "inspect_pages",
                "requested_pages": pages,
                "decision_source": inspection.decision_source,
                "decisions": [
                    decision.model_dump(mode="json") for decision in inspection.decisions
                ],
            },
        )
        chunks = [
            chunk.model_copy(update={"route_score": routed_document.keyword_score})
            for chunk in inspection.chunks
        ]
        return FocusedSearchResult(
            group_ref=group.group_ref,
            document_id=doc.doc_id,
            chunks=chunks,
            warnings=warnings,
            inspected_node_count=len(inspected_node_ids),
            inspected_page_count=len(inspection.evaluated_pages),
            llm_request_count=tree_calls + inspection.llm_request_count,
            degraded=degraded or inspection.decision_source != "llm",
        )

    @staticmethod
    def _trace(
        collector: Any | None,
        *,
        group_ref: str,
        document_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        if collector is not None and getattr(collector, "enabled", False):
            collector.record_detail(
                node="focused_search",
                kind=kind,
                payload=payload,
                group_ref=group_ref,
                document_id=document_id,
            )

    async def _fetch_nodes(self, *, user_id: str, kb_id: str, doc_id: str) -> list[NodeRecord]:
        kwargs = {
            "user_id": user_id,
            "kb_id": kb_id,
            "doc_id": doc_id,
            "session_id": self.session_id,
        }
        if self.db_executor is None:
            return self.repository.fetch_nodes(**kwargs)
        return await self.db_executor.run(self.repository.fetch_nodes, **kwargs)

    async def _scan(self, *, doc_id: str, root_node_id: str, level: int) -> NodeTreeScanResult:
        kwargs = {"doc_id": doc_id, "root_node_id": root_node_id, "level": level}
        if self.db_executor is None:
            return self.scanner.scan(**kwargs)
        return await self.db_executor.run(self.scanner.scan, **kwargs)

    async def _call_agent(
        self,
        *,
        request_id: str,
        document_id: str,
        messages: list[dict[str, Any]],
        step: int,
    ) -> tuple[LLMToolCall, ...]:
        result = await self.gateway.complete_json(
            LLMRequest(
                request_id=request_id,
                phase=f"tree_navigation:{document_id}:{step + 1}",
                messages=list(messages),
                model=self.model,
                max_tokens=self.tree_output_tokens,
                tools=TREE_NAVIGATION_TOOLS,
                tool_choice="required",
                parallel_tool_calls=True,
            )
        )
        if not result.tool_calls:
            raise ValueError("tree navigation returned no tool calls")
        return result.tool_calls

    @staticmethod
    def _initial_messages(
        *,
        group: QueryGroup,
        document_id: str,
        roots: list[NodeRecord],
        scans: list[NodeTreeScanResult],
    ) -> list[dict[str, Any]]:
        initial_calls = tuple(
            LLMToolCall(
                call_id=f"initial-scan-{index + 1}",
                name="scan_node_tree",
                arguments={"root_node_id": root.node_id, "level": 1},
            )
            for index, root in enumerate(roots)
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": TREE_NAVIGATION_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "group_ref": group.group_ref,
                        "queries": group.queries,
                        "document_id": document_id,
                    },
                    ensure_ascii=False,
                ),
            },
            _assistant_tool_call_message(initial_calls),
        ]
        messages.extend(
            _tool_result_message(call=call, result=result)
            for call, result in zip(initial_calls, scans, strict=True)
        )
        return messages

    @staticmethod
    def _full_pages(page_count: int, records: list[NodeRecord]) -> list[int]:
        if page_count > 0:
            return list(range(1, page_count + 1))
        maximum = max((node.end_page or 0 for node in records), default=0)
        return list(range(1, maximum + 1))


def _pages_for_nodes(node_ids: list[str], known: dict[str, NodeView]) -> list[int]:
    return sorted(
        {
            page
            for node_id in node_ids
            for page in range(known[node_id].start_page, known[node_id].end_page + 1)
        }
    )


def _assistant_tool_call_message(tool_calls: tuple[LLMToolCall, ...]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in tool_calls
        ],
    }


def _tool_result_message(
    *, call: LLMToolCall, result: NodeTreeScanResult
) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call.call_id,
        "name": call.name,
        "content": result.model_dump_json(),
    }


def _unique_nodes(nodes: list[NodeView]) -> list[NodeView]:
    return list({node.node_id: node for node in nodes}.values())
