import httpx
import pytest
import respx

from app.api.contracts import ChatCompletionRequest
from app.chat.models import RetrievalStatus
from app.integrations import retrieval as retrieval_module
from app.integrations.retrieval import RetrievalClient
from app.platform.errors import ApiError


def test_dynamic_return_budget_is_bounded_and_scales_with_request_scope() -> None:
    assert hasattr(retrieval_module, "calculate_max_return_tokens")
    calculate_max_return_tokens = retrieval_module.calculate_max_return_tokens
    assert calculate_max_return_tokens("审批流程是什么", document_count=1) == 8192
    assert calculate_max_return_tokens("审批流程是什么", document_count=12) == 16384
    assert (
        calculate_max_return_tokens(
            "请详细总结并比较这些文档",
            document_count=50,
            configured_max=180_000,
        )
        == 180_000
    )
    assert (
        calculate_max_return_tokens(
            "请详细总结并比较这些文档",
            document_count=50,
            requested_max=262_144,
            configured_max=300_000,
        )
        == 262_144
    )


def test_dynamic_top_k_is_bounded_and_scales_with_query_and_scope() -> None:
    assert retrieval_module.calculate_top_k("审批流程是什么", document_count=1) == 4
    assert retrieval_module.calculate_top_k("审批流程是什么", document_count=6) == 8
    assert retrieval_module.calculate_top_k("审批流程是什么", document_count=12) == 12
    assert retrieval_module.calculate_top_k("请详细总结并比较这些文档", document_count=2) == 20
    assert (
        retrieval_module.calculate_top_k("请详细总结并比较这些文档", document_count=20, requested_max=9) == 9
    )


@pytest.mark.asyncio
async def test_document_name_lookup_uses_batch_meta_endpoint_and_input_order() -> None:
    response = {
        "documents": [
            {"doc_id": "doc-2", "doc_name": "B.pdf"},
            {"doc_id": "temp-1", "doc_name": "临时文档.pdf", "is_temporary": True},
            {"doc_id": "doc-1", "doc_name": "A.pdf"},
        ],
        "missing_doc_ids": [],
    }
    with respx.mock:
        route = respx.post("http://retrieval/rag/v1/documents/meta").mock(
            return_value=httpx.Response(200, json=response)
        )
        async with httpx.AsyncClient() as http:
            names = await RetrievalClient(base_url="http://retrieval", http_client=http).get_document_names(
                user_id="user-1",
                kb_id="kb-1",
                requested_doc_ids=["doc-1", "temp-1", "doc-2"],
                permanent_scope_ids=["doc-1", "doc-2"],
                temporary_scope_ids=["temp-1"],
                session_id="session-1",
            )

    payload = __import__("json").loads(route.calls[0].request.content)
    assert payload == {
        "user_id": "user-1",
        "kb_id": "kb-1",
        "doc_ids": ["doc-1", "doc-2"],
        "temp_doc_ids": ["temp-1"],
        "session_id": "session-1",
        "include_missing": True,
    }
    assert names == ["A.pdf", "临时文档.pdf", "B.pdf"]


@pytest.mark.asyncio
async def test_document_name_lookup_rejects_incomplete_metadata() -> None:
    response = {
        "documents": [{"doc_id": "doc-1", "doc_name": "A.pdf"}],
        "missing_doc_ids": ["doc-2"],
    }
    with respx.mock:
        respx.post("http://retrieval/rag/v1/documents/meta").mock(
            return_value=httpx.Response(200, json=response)
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ApiError, match="incremental document metadata"):
                await RetrievalClient(base_url="http://retrieval", http_client=http).get_document_names(
                    user_id="user-1",
                    kb_id="kb-1",
                    requested_doc_ids=["doc-1", "doc-2"],
                    permanent_scope_ids=["doc-1", "doc-2"],
                    temporary_scope_ids=[],
                    session_id=None,
                )


@pytest.mark.asyncio
async def test_retrieval_uses_real_contract_and_forwards_key(base_payload) -> None:
    request = ChatCompletionRequest.model_validate(base_payload)
    response = {
        "chunks": [
            {
                "chunk_id": "c1",
                "document_id": "doc-1",
                "document_name": "制度.pdf",
                "path": "1",
                "content": "审批需要两级确认。",
                "score": 0.9,
                "source_type": "node",
                "document_meta": None,
                "chunk_meta": {},
            }
        ],
        "usage": {
            "candidate_count": 1,
            "returned_count": 1,
            "latency_ms": 10,
            "actual_mode": "semantic",
            "llm_request_count": 1,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "debug": None,
    }
    with respx.mock:
        route = respx.post("http://retrieval/rag/v1/retrieve").mock(
            return_value=httpx.Response(200, json=response, headers={"X-Request-Id": "retr-1"})
        )
        async with httpx.AsyncClient() as http:
            client = RetrievalClient(base_url="http://retrieval", http_client=http)
            result = await client.retrieve(
                request=request,
                query="独立问题",
                include_debug=False,
            )

    sent = route.calls[0].request
    payload = __import__("json").loads(sent.content)
    assert payload["search_mode"] == "hybrid"
    assert payload["user_id"] == "user-1"
    assert payload["doc_ids"] == ["doc-1"]
    assert payload["session_id"] is None
    assert payload["top_k"] == 4
    assert payload["max_return_tokens"] == 8192
    assert "Authorization" not in sent.headers
    assert result.status is RetrievalStatus.SUCCESS
    assert result.request_id == "retr-1"


@pytest.mark.asyncio
async def test_retrieval_forwards_preprocessed_query_list_without_joining(base_payload) -> None:
    request = ChatCompletionRequest.model_validate(base_payload)
    response = {"chunks": [], "usage": {"returned_count": 0, "latency_ms": 1}}
    with respx.mock:
        route = respx.post("http://retrieval/rag/v1/retrieve").mock(
            return_value=httpx.Response(200, json=response)
        )
        async with httpx.AsyncClient() as http:
            await RetrievalClient(base_url="http://retrieval", http_client=http).retrieve(
                request=request,
                query=["你能看到哪些文档？", "A 公司营业额是多少？"],
                include_debug=False,
            )

    payload = __import__("json").loads(route.calls[0].request.content)
    assert payload["query"] == ["你能看到哪些文档？", "A 公司营业额是多少？"]
    assert 4 <= payload["top_k"] <= 20
    assert 4096 <= payload["max_return_tokens"] <= 180_000


@pytest.mark.asyncio
async def test_retrieval_uses_configured_dynamic_return_budget_limit(base_payload) -> None:
    request = ChatCompletionRequest.model_validate(base_payload)
    response = {"chunks": [], "usage": {"returned_count": 0, "latency_ms": 1}}
    with respx.mock:
        route = respx.post("http://retrieval/rag/v1/retrieve").mock(
            return_value=httpx.Response(200, json=response)
        )
        async with httpx.AsyncClient() as http:
            await RetrievalClient(
                base_url="http://retrieval",
                http_client=http,
                max_return_tokens=180_000,
            ).retrieve(
                request=request,
                query="请详细总结并比较这些文档",
                include_debug=False,
            )

    payload = __import__("json").loads(route.calls[0].request.content)
    assert payload["max_return_tokens"] == 180_000


@pytest.mark.asyncio
async def test_retrieval_forwards_explicit_return_budget_without_chat_side_clamping(
    base_payload,
) -> None:
    base_payload["rag"]["max_return_tokens"] = 262_144
    request = ChatCompletionRequest.model_validate(base_payload)
    response = {"chunks": [], "usage": {"returned_count": 0, "latency_ms": 1}}
    with respx.mock:
        route = respx.post("http://retrieval/rag/v1/retrieve").mock(
            return_value=httpx.Response(200, json=response)
        )
        async with httpx.AsyncClient() as http:
            await RetrievalClient(base_url="http://retrieval", http_client=http).retrieve(
                request=request,
                query="问题",
                include_debug=False,
            )

    payload = __import__("json").loads(route.calls[0].request.content)
    assert payload["max_return_tokens"] == 262_144


@pytest.mark.asyncio
async def test_retrieval_validation_error_uses_standard_http_error_mapping(base_payload) -> None:
    request = ChatCompletionRequest.model_validate(base_payload)
    details = {
        "errors": [
            {
                "loc": ["body", "max_return_tokens"],
                "msg": "Input should be less than or equal to 262144",
                "type": "less_than_equal",
            }
        ]
    }
    with respx.mock:
        respx.post("http://retrieval/rag/v1/retrieve").mock(
            return_value=httpx.Response(
                422,
                json={
                    "error": {
                        "code": "REQUEST_VALIDATION_ERROR",
                        "message": "request body validation failed",
                        "retryable": False,
                        "details": details,
                    }
                },
            )
        )
        async with httpx.AsyncClient() as http:
            result = await RetrievalClient(base_url="http://retrieval", http_client=http).retrieve(
                request=request,
                query="问题",
                include_debug=False,
            )

    assert result.error == {
        "code": "retrieval_http_error",
        "status_code": 422,
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_retrieval_forwards_temporary_document_session_id(base_payload) -> None:
    base_payload["rag"].update(
        {
            "doc_ids": [],
            "temp_doc_ids": ["temp-doc-1"],
            "session_id": "upload-session-1",
        }
    )
    request = ChatCompletionRequest.model_validate(base_payload)
    response = {
        "chunks": [],
        "usage": {
            "returned_count": 0,
            "latency_ms": 1,
            "actual_mode": "semantic",
            "llm_request_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
    with respx.mock:
        route = respx.post("http://retrieval/rag/v1/retrieve").mock(
            return_value=httpx.Response(200, json=response)
        )
        async with httpx.AsyncClient() as http:
            await RetrievalClient(base_url="http://retrieval", http_client=http).retrieve(
                request=request,
                query="临时文档讲了什么",
                include_debug=False,
            )

    payload = __import__("json").loads(route.calls[0].request.content)
    assert payload["temp_doc_ids"] == ["temp-doc-1"]
    assert payload["session_id"] == "upload-session-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 403, 429, 500, 503])
async def test_all_retrieval_http_errors_are_degradable(base_payload, status_code) -> None:
    request = ChatCompletionRequest.model_validate(base_payload)
    with respx.mock:
        respx.post("http://retrieval/rag/v1/retrieve").mock(
            return_value=httpx.Response(status_code, json={"error": {"code": "failure"}})
        )
        async with httpx.AsyncClient() as http:
            result = await RetrievalClient(base_url="http://retrieval", http_client=http).retrieve(
                request=request,
                query="问题",
                include_debug=False,
            )

    assert result.status is RetrievalStatus.ERROR
    assert result.error["status_code"] == status_code


@pytest.mark.asyncio
async def test_retrieval_not_found_is_no_result_instead_of_service_unavailable(
    base_payload,
) -> None:
    request = ChatCompletionRequest.model_validate(base_payload)
    with respx.mock:
        respx.post("http://retrieval/rag/v1/retrieve").mock(
            return_value=httpx.Response(
                404,
                json={
                    "error": {
                        "code": "DOCUMENT_NOT_FOUND",
                        "message": "one or more requested documents are not accessible",
                    }
                },
                headers={"X-Request-Id": "retrieval-404"},
            )
        )
        async with httpx.AsyncClient() as http:
            result = await RetrievalClient(base_url="http://retrieval", http_client=http).retrieve(
                request=request,
                query="问题",
                include_debug=False,
            )

    assert result.status is RetrievalStatus.NO_RESULT
    assert result.request_id == "retrieval-404"
    assert result.error == {
        "code": "DOCUMENT_NOT_FOUND",
        "status_code": 404,
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_empty_chunks_is_no_result(base_payload) -> None:
    request = ChatCompletionRequest.model_validate(base_payload)
    response = {
        "chunks": [],
        "usage": {
            "candidate_count": 0,
            "returned_count": 0,
            "latency_ms": 1,
            "actual_mode": "keyword",
            "llm_request_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
    with respx.mock:
        respx.post("http://retrieval/rag/v1/retrieve").mock(return_value=httpx.Response(200, json=response))
        async with httpx.AsyncClient() as http:
            result = await RetrievalClient(base_url="http://retrieval", http_client=http).retrieve(
                request=request,
                query="问题",
                include_debug=False,
            )

    assert result.status is RetrievalStatus.NO_RESULT
