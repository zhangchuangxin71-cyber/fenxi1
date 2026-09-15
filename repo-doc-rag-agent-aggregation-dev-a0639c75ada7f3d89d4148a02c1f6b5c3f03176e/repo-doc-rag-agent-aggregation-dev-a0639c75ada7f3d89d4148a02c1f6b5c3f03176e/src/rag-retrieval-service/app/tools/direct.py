from __future__ import annotations

import json
from collections import defaultdict
from typing import Protocol

from app.api.schemas import RetrievalWarning
from app.core.models import CandidateChunk, ToolOutput
from app.db.repositories import DocumentProfile, NodeRecord, PageRecord


class DirectRepository(Protocol):
    def fetch_pages(
        self, *, user_id: str, kb_id: str, doc_id: str, pages: list[int] | None = None
    ) -> list[PageRecord]: ...

    def fetch_nodes(self, *, user_id: str, kb_id: str, doc_id: str) -> list[NodeRecord]: ...


def _page_chunk(
    *,
    page: PageRecord,
    group_ref: str,
    questions: list[str],
    chapter_titles: list[str] | None = None,
) -> CandidateChunk:
    return CandidateChunk(
        chunk_id=f"{page.doc_id}:page:{page.page_number}",
        document_id=page.doc_id,
        document_ids=[page.doc_id],
        document_name=page.document_name,
        page_number=page.page_number,
        path=f"document:{page.doc_id}:page:{page.page_number}",
        content=page.content,
        source_type="page",
        category="routed_direct",
        group_matches={group_ref: "accept"},
        questions_by_group={group_ref: list(questions)},
        chunk_meta={"chapter_titles": chapter_titles or []},
    )


class DirectToolExecutor:
    def __init__(
        self,
        *,
        repository: DirectRepository,
        user_id: str,
        kb_id: str,
        session_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.user_id = user_id
        self.kb_id = kb_id
        self.session_id = session_id

    def get_docs_metainfo(
        self,
        *,
        group_ref: str,
        questions: list[str],
        profiles: list[DocumentProfile],
        doc_ids: list[str],
        fields: list[str],
    ) -> ToolOutput:
        selected = [profile for profile in profiles if profile.doc_id in set(doc_ids)]
        entries: list[dict[str, object]] = []
        for profile in selected:
            entry: dict[str, object] = {}
            if "doc_name" in fields:
                entry["doc_name"] = profile.doc_name
            if "page_count" in fields:
                entry["page_count"] = profile.page_count
            if "chapter_count" in fields:
                entry["chapter_count"] = profile.node_count
            entries.append(entry)
        warnings = []
        if not selected and doc_ids:
            warnings.append(_missing_resource_warning(group_ref, doc_ids))
        chunk = CandidateChunk(
            chunk_id=f"direct:{group_ref}:metainfo",
            document_ids=[profile.doc_id for profile in selected],
            path="document:metainfo",
            content=json.dumps(entries, ensure_ascii=False),
            source_type="scope_metadata",
            category="routed_direct",
            group_matches={group_ref: "accept"},
            questions_by_group={group_ref: questions},
        )
        return ToolOutput(
            chunks=[chunk] if selected else [], warnings=warnings, coverage_complete=not warnings
        )

    def get_docs_description(
        self,
        *,
        group_ref: str,
        questions: list[str],
        profiles: list[DocumentProfile],
        doc_ids: list[str],
    ) -> ToolOutput:
        selected = [profile for profile in profiles if profile.doc_id in set(doc_ids)]
        warnings = []
        if not selected and doc_ids:
            warnings.append(_missing_resource_warning(group_ref, doc_ids))
        chunks = [
            CandidateChunk(
                chunk_id=f"{profile.doc_id}:description",
                document_id=profile.doc_id,
                document_ids=[profile.doc_id],
                document_name=profile.doc_name,
                path="document:description",
                content=f"这是《{profile.doc_name}》的简要概览：{profile.doc_description}",
                source_type="document_overview",
                category="routed_direct",
                group_matches={group_ref: "accept"},
                questions_by_group={group_ref: questions},
            )
            for profile in selected
        ]
        return ToolOutput(chunks=chunks, warnings=warnings, coverage_complete=not warnings)

    def get_content_of_pages(
        self, *, group_ref: str, questions: list[str], pages_by_doc: dict[str, list[int]]
    ) -> ToolOutput:
        chunks: list[CandidateChunk] = []
        warnings: list[RetrievalWarning] = []
        inspected_page_count = 0
        for doc_id, pages in pages_by_doc.items():
            requested_pages = sorted({int(page) for page in pages if int(page) > 0})
            records = self.repository.fetch_pages(
                user_id=self.user_id,
                kb_id=self.kb_id,
                doc_id=doc_id,
                pages=pages,
                session_id=self.session_id,
            )
            inspected_page_count += len(records)
            found_pages = {record.page_number for record in records}
            missing_pages = [page for page in requested_pages if page not in found_pages]
            if missing_pages:
                warnings.append(
                    _missing_resource_warning(
                        group_ref,
                        [doc_id],
                        detail=f"missing pages: {missing_pages}",
                    )
                )
            chunks.extend(
                _page_chunk(page=page, group_ref=group_ref, questions=questions) for page in records
            )
        return ToolOutput(
            chunks=chunks,
            warnings=warnings,
            coverage_complete=not warnings,
            inspected_page_count=inspected_page_count,
        )

    def get_content_of_chapters(
        self, *, group_ref: str, questions: list[str], node_ids_by_doc: dict[str, list[str]]
    ) -> ToolOutput:
        chunks: list[CandidateChunk] = []
        warnings: list[RetrievalWarning] = []
        inspected_node_count = 0
        inspected_page_count = 0
        for doc_id, node_ids in node_ids_by_doc.items():
            wanted = set(node_ids)
            all_nodes = self.repository.fetch_nodes(
                user_id=self.user_id,
                kb_id=self.kb_id,
                doc_id=doc_id,
                session_id=self.session_id,
            )
            inspected_node_count += len(all_nodes)
            nodes = [
                node
                for node in all_nodes
                if node.node_id in wanted
                and node.start_page is not None
                and node.end_page is not None
            ]
            missing_nodes = sorted(wanted - {node.node_id for node in nodes})
            if missing_nodes:
                warnings.append(
                    _missing_resource_warning(
                        group_ref,
                        [doc_id],
                        detail=f"missing chapter nodes: {missing_nodes}",
                    )
                )
            pages = sorted(
                {
                    page
                    for node in nodes
                    for page in range(int(node.start_page), int(node.end_page) + 1)
                }
            )
            records = self.repository.fetch_pages(
                user_id=self.user_id,
                kb_id=self.kb_id,
                doc_id=doc_id,
                pages=pages,
                session_id=self.session_id,
            )
            inspected_page_count += len(records)
            titles = [node.title for node in nodes]
            chunks.extend(
                _page_chunk(
                    page=page,
                    group_ref=group_ref,
                    questions=questions,
                    chapter_titles=titles,
                )
                for page in records
            )
        return ToolOutput(
            chunks=chunks,
            warnings=warnings,
            coverage_complete=not warnings,
            inspected_node_count=inspected_node_count,
            inspected_page_count=inspected_page_count,
        )

    def view_doc_title_tree(
        self,
        *,
        group_ref: str,
        questions: list[str],
        profiles: list[DocumentProfile],
        doc_ids: list[str],
        level: int,
        include_node_id: bool,
    ) -> ToolOutput:
        profile_by_id = {profile.doc_id: profile for profile in profiles}
        chunks: list[CandidateChunk] = []
        inspected_node_count = 0
        for doc_id in doc_ids:
            nodes = self.repository.fetch_nodes(
                user_id=self.user_id,
                kb_id=self.kb_id,
                doc_id=doc_id,
                session_id=self.session_id,
            )
            inspected_node_count += len(nodes)
            content = json.dumps(
                _tree(nodes=nodes, max_level=max(0, level), include_node_id=include_node_id),
                ensure_ascii=False,
            )
            profile = profile_by_id.get(doc_id)
            chunks.append(
                CandidateChunk(
                    chunk_id=f"{doc_id}:title-tree:{level}",
                    document_id=doc_id,
                    document_ids=[doc_id],
                    document_name=profile.doc_name if profile else None,
                    path=f"document:{doc_id}:title-tree",
                    content=content,
                    source_type="title_tree",
                    category="routed_direct",
                    group_matches={group_ref: "accept"},
                    questions_by_group={group_ref: questions},
                    chunk_meta={"level": level, "include_node_id": include_node_id},
                )
            )
        return ToolOutput(chunks=chunks, inspected_node_count=inspected_node_count)


def _missing_resource_warning(
    group_ref: str, document_ids: list[str], *, detail: str = "requested resource was not found"
) -> RetrievalWarning:
    return RetrievalWarning(
        code="DIRECT_RESOURCE_NOT_FOUND",
        message=detail,
        affected_group_refs=[group_ref],
        affected_document_ids=list(dict.fromkeys(document_ids)),
    )


def _tree(*, nodes: list[NodeRecord], max_level: int, include_node_id: bool) -> list[dict]:
    selected = [node for node in nodes if (node.level or 0) <= max_level]
    selected_ids = {node.node_id for node in selected}
    children: dict[str | None, list[NodeRecord]] = defaultdict(list)
    for node in selected:
        parent = node.parent_node_id if node.parent_node_id in selected_ids else None
        children[parent].append(node)
    for values in children.values():
        values.sort(key=lambda node: (node.sibling_order, node.node_id))

    def render(node: NodeRecord) -> dict:
        value: dict[str, object] = {
            "title": node.title,
            "summary": node.summary,
            "level": node.level,
            "start_page": node.start_page,
            "end_page": node.end_page,
            "children": [render(child) for child in children.get(node.node_id, [])],
        }
        if include_node_id:
            value["node_id"] = node.node_id
        return value

    return [render(root) for root in children.get(None, [])]
