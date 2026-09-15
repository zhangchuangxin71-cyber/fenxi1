from __future__ import annotations

import json
from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.db.repositories import NodeRecord


class NodeRepository(Protocol):
    def fetch_nodes(self, *, user_id: str, kb_id: str, doc_id: str) -> list[NodeRecord]: ...


class NodeView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    parent_node_id: str | None
    title: str
    summary: str
    level: int
    start_page: int
    end_page: int
    child_count: int


class NodeTreeScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str
    root_node_id: str
    requested_level: int
    status: Literal["ok", "too_large", "unusable"]
    result_complete: bool
    nodes: list[NodeView] = Field(default_factory=list)
    total_node_count: int = 0
    root_start_page: int | None = None
    root_end_page: int | None = None


class NodeTreeScanner:
    def __init__(
        self,
        *,
        repository: NodeRepository,
        user_id: str,
        kb_id: str,
        count_tokens: Callable[[str], int],
        max_result_tokens: int,
        session_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.user_id = user_id
        self.kb_id = kb_id
        self.count_tokens = count_tokens
        self.max_result_tokens = max(1, int(max_result_tokens))
        self.session_id = session_id

    def scan(self, *, doc_id: str, root_node_id: str, level: int) -> NodeTreeScanResult:
        records = self.repository.fetch_nodes(
            user_id=self.user_id,
            kb_id=self.kb_id,
            doc_id=doc_id,
            session_id=self.session_id,
        )
        by_id = {node.node_id: node for node in records}
        root = by_id.get(root_node_id)
        if root is None or root.level is None or root.start_page is None or root.end_page is None:
            return NodeTreeScanResult(
                doc_id=doc_id,
                root_node_id=root_node_id,
                requested_level=level,
                status="unusable",
                result_complete=False,
            )
        target_level = int(root.level) + max(0, int(level))
        descendants = _descendants(root_node_id=root_node_id, records=records)
        selected = [
            node
            for node in records
            if node.node_id in descendants
            and node.level == target_level
            and node.start_page is not None
            and node.end_page is not None
            and int(node.start_page) > 0
            and int(node.end_page) >= int(node.start_page)
        ]
        selected.sort(key=lambda node: (node.sibling_order, node.node_id))
        views = [
            NodeView(
                node_id=node.node_id,
                parent_node_id=node.parent_node_id,
                title=node.title,
                summary=node.summary,
                level=int(node.level or target_level),
                start_page=int(node.start_page or 0),
                end_page=int(node.end_page or 0),
                child_count=node.child_count,
            )
            for node in selected
        ]
        serialized = json.dumps([view.model_dump() for view in views], ensure_ascii=False)
        if self.count_tokens(serialized) > self.max_result_tokens:
            return NodeTreeScanResult(
                doc_id=doc_id,
                root_node_id=root_node_id,
                requested_level=level,
                status="too_large",
                result_complete=False,
                total_node_count=len(views),
                root_start_page=int(root.start_page),
                root_end_page=int(root.end_page),
            )
        return NodeTreeScanResult(
            doc_id=doc_id,
            root_node_id=root_node_id,
            requested_level=level,
            status="ok" if views else "unusable",
            result_complete=True,
            nodes=views,
            total_node_count=len(views),
            root_start_page=int(root.start_page),
            root_end_page=int(root.end_page),
        )


def _descendants(*, root_node_id: str, records: list[NodeRecord]) -> set[str]:
    children: dict[str, list[str]] = {}
    for node in records:
        if node.parent_node_id is not None:
            children.setdefault(node.parent_node_id, []).append(node.node_id)
    found: set[str] = set()
    pending = list(children.get(root_node_id, []))
    while pending:
        node_id = pending.pop()
        if node_id in found:
            continue
        found.add(node_id)
        pending.extend(children.get(node_id, []))
    return found
