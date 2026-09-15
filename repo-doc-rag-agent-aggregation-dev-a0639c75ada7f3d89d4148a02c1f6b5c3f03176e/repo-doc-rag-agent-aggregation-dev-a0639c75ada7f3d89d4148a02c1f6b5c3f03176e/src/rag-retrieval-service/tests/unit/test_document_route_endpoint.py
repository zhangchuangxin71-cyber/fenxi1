from __future__ import annotations

from app.api.schemas import DocumentRouteRequest
from app.db.repositories import DocumentProfile
from app.llm.gateway import GatewayRequestStats
from app.workflows.document_routing.endpoint import DocumentRouteEndpointService
from app.workflows.document_routing.models import (
    DocumentRoute,
    PrefilterCandidate,
    PrefilterResult,
    RoutedDocument,
)
from app.workflows.document_routing.prefilter import KeywordPrefilterRun


def _document(doc_id: str) -> DocumentProfile:
    return DocumentProfile(
        doc_id=doc_id,
        doc_name=f"{doc_id} name",
        doc_description=f"{doc_id} summary",
    )


def _request(*, keyword_prefilter: bool = False) -> DocumentRouteRequest:
    return DocumentRouteRequest(
        user_id="u1",
        kb_id="kb1",
        doc_ids=["d1", "d2", "d3"],
        criteria=[
            {
                "queries": ["新能源汽车产业趋势"],
                "target_docs_description": "新能源汽车产业趋势材料",
                "target_docs_keywords": ["新能源汽车", "产业趋势"],
            }
        ],
        keyword_prefilter=keyword_prefilter,
    )


class Repository:
    def __init__(self) -> None:
        self.documents = [_document("d3"), _document("d1"), _document("d2")]

    def fetch_scope(self, **_):
        return self.documents, []


class Gateway:
    def __init__(self) -> None:
        self.request_ids: list[str] = []
        self.cleared: list[str] = []

    def begin_request(self, request_id: str, *, debug_enabled: bool) -> None:
        assert debug_enabled is True
        self.request_ids.append(request_id)

    def stats(self, request_id: str) -> GatewayRequestStats:
        assert request_id == self.request_ids[-1]
        return GatewayRequestStats(request_count=2, prompt_tokens=11, completion_tokens=7)

    def clear_stats(self, request_id: str) -> None:
        self.cleared.append(request_id)


class KeywordPrefilter:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, *, groups, documents) -> KeywordPrefilterRun:
        self.calls += 1
        candidate = PrefilterCandidate(
            document=next(document for document in documents if document.doc_id == "d2"),
            keyword_score=0.75,
            components={"name_phrase": 0.45, "name_coverage": 0.30},
        )
        return KeywordPrefilterRun(
            results={
                groups[0].group_ref: PrefilterResult(
                    group_ref=groups[0].group_ref,
                    candidates=[candidate],
                    rejected_count=2,
                )
            },
            overflow_group_refs=[],
        )


class Router:
    def __init__(self) -> None:
        self.prefiltered = None
        self.documents = None

    async def route(self, *, request_id, groups, documents, prefiltered):
        assert request_id.startswith("route_")
        self.prefiltered = prefiltered
        self.documents = documents
        result = prefiltered[groups[0].group_ref]
        candidates = result.candidates
        accept = [
            RoutedDocument(
                document=candidates[0].document,
                grade="accept",
                keyword_score=candidates[0].keyword_score,
                decision_source="llm",
            )
        ]
        possible = []
        if len(candidates) > 1:
            possible.append(
                RoutedDocument(
                    document=candidates[1].document,
                    grade="possible",
                    keyword_score=candidates[1].keyword_score,
                    decision_source="llm",
                )
            )
        route = DocumentRoute(
            group_ref=groups[0].group_ref,
            accept_docs=accept,
            possible_docs=possible,
            rejected_document_count=result.rejected_count + max(0, len(candidates) - 2),
            prefilter_candidate_count=len(candidates),
            prefilter_rejected_count=result.rejected_count,
        )
        return [route], []


def _service(prefilter: KeywordPrefilter, router: Router, gateway: Gateway):
    return DocumentRouteEndpointService(
        repository=Repository(),
        db_executor=None,
        keyword_prefilter=prefilter,
        router=router,
        gateway=gateway,
        max_doc_ids=100,
    )


async def test_default_skips_keyword_prefilter_and_routes_every_document() -> None:
    prefilter = KeywordPrefilter()
    router = Router()
    gateway = Gateway()

    response = await _service(prefilter, router, gateway).route(_request())

    assert prefilter.calls == 0
    assert [document.doc_id for document in router.documents] == ["d1", "d2", "d3"]
    candidates = router.prefiltered["g0001"].candidates
    assert [candidate.document.doc_id for candidate in candidates] == ["d1", "d2", "d3"]
    assert all(candidate.keyword_score == 0.0 for candidate in candidates)
    assert response.groups[0].accept_doc_ids == ["d1"]
    assert response.groups[0].possible_doc_ids == ["d2"]
    assert response.groups[0].reject_doc_ids == ["d3"]
    assert response.usage.llm_request_count == 2
    assert gateway.cleared == gateway.request_ids


async def test_enabled_keyword_prefilter_passes_its_exact_candidates_to_router() -> None:
    prefilter = KeywordPrefilter()
    router = Router()
    gateway = Gateway()

    response = await _service(prefilter, router, gateway).route(_request(keyword_prefilter=True))

    assert prefilter.calls == 1
    candidates = router.prefiltered["g0001"].candidates
    assert [candidate.document.doc_id for candidate in candidates] == ["d2"]
    assert candidates[0].keyword_score == 0.75
    assert response.groups[0].accept_doc_ids == ["d2"]
    assert response.groups[0].possible_doc_ids == []
    assert response.groups[0].reject_doc_ids == ["d1", "d3"]
