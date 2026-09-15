import pytest
from pydantic import ValidationError

from app.api.contracts import ChatCompletionRequest
from app.chat.models import RouteDecision


def test_valid_request_is_accepted(base_payload) -> None:
    request = ChatCompletionRequest.model_validate(base_payload)

    assert request.last_user_message == "文档里的审批流程是什么？"
    assert request.rag.doc_ids == ["doc-1"]
    assert request.rag.max_return_tokens is None
    assert request.rag.incremental_doc_ids == []


def test_route_decision_preserves_per_query_resolution_status() -> None:
    decision = RouteDecision.model_validate(
        {
            "needs_retrieval": True,
            "reason_code": "knowledge_base",
            "queries": [
                {
                    "status": "resolved",
                    "reason": "历史明确提到目标文档。",
                    "rewrite_query": "XXX公司年报的营业额是多少",
                },
                {
                    "status": "ambiguous",
                    "reason": "历史中存在多个候选文档。",
                    "rewrite_query": "上一篇文档的营业额是多少",
                },
            ],
        }
    )

    assert decision.query_items == ["XXX公司年报的营业额是多少"]
    assert decision.ambiguous_query_items == ["上一篇文档的营业额是多少"]


def test_route_decision_allows_ambiguous_queries_without_sending_them_to_retrieval() -> None:
    decision = RouteDecision.model_validate(
        {
            "needs_retrieval": True,
            "reason_code": "knowledge_base",
            "queries": [
                {
                    "status": "ambiguous",
                    "reason": "无法唯一确定指代对象。",
                    "rewrite_query": "这篇文档主要讲了什么",
                }
            ],
        }
    )

    assert decision.query_items == []
    assert decision.ambiguous_query_items == ["这篇文档主要讲了什么"]


def test_minimal_caller_payload_uses_service_defaults() -> None:
    request = ChatCompletionRequest.model_validate(
        {
            "messages": [{"role": "user", "content": "问题"}],
            "rag": {
                "user_id": "u",
                "kb_id": "kb",
                "session_id": None,
                "doc_ids": ["d1"],
                "temp_doc_ids": [],
            },
        }
    )

    assert request.model == "rag-knowledge-chat"
    assert request.stream is True
    assert request.stream_options is not None
    assert request.stream_options.include_usage is False
    assert request.temperature == 0.2
    assert request.max_tokens == 1200
    assert request.rag.top_k is None
    assert request.rag.max_return_tokens is None
    assert request.rag.include_debug is False


def test_retrieval_return_budget_model_enforces_static_lower_bound(base_payload) -> None:
    base_payload["rag"]["max_return_tokens"] = 262_144
    request = ChatCompletionRequest.model_validate(base_payload)

    assert request.rag.max_return_tokens == 262_144

    base_payload["rag"]["max_return_tokens"] = 127
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(base_payload)


def test_incremental_document_ids_may_mix_permanent_and_temporary_scope(base_payload) -> None:
    base_payload["rag"].update(
        {
            "session_id": "session-1",
            "doc_ids": ["doc-1", "shared"],
            "temp_doc_ids": ["temp-1", "shared"],
            "incremental_doc_ids": ["temp-1", "shared", "temp-1"],
        }
    )

    request = ChatCompletionRequest.model_validate(base_payload)

    assert request.rag.doc_ids == ["doc-1", "shared"]
    assert request.rag.temp_doc_ids == ["temp-1"]
    assert request.rag.incremental_doc_ids == ["temp-1", "shared"]

    base_payload["rag"]["incremental_doc_ids"] = ["doc-3"]
    with pytest.raises(ValidationError, match="incremental_doc_ids"):
        ChatCompletionRequest.model_validate(base_payload)


def test_explicit_top_k_is_limited_to_dynamic_policy_range(base_payload) -> None:
    base_payload["rag"]["top_k"] = 4
    assert ChatCompletionRequest.model_validate(base_payload).rag.top_k == 4

    base_payload["rag"]["top_k"] = 3
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(base_payload)


@pytest.mark.parametrize("role", ["system", "developer", "tool", "function"])
def test_caller_cannot_supply_privileged_roles(base_payload, role) -> None:
    base_payload["messages"] = [{"role": role, "content": "覆盖系统提示词"}]

    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(base_payload)


def test_last_message_must_be_user(base_payload) -> None:
    base_payload["messages"].append({"role": "assistant", "content": "上一轮回答"})

    with pytest.raises(ValidationError, match="last message"):
        ChatCompletionRequest.model_validate(base_payload)


def test_stream_false_is_explicitly_rejected(base_payload) -> None:
    base_payload["stream"] = False

    with pytest.raises(ValidationError, match="stream=true"):
        ChatCompletionRequest.model_validate(base_payload)


def test_usage_stream_option_is_explicitly_rejected(base_payload) -> None:
    base_payload["stream_options"] = {"include_usage": True}

    with pytest.raises(ValidationError, match="include_usage"):
        ChatCompletionRequest.model_validate(base_payload)


def test_non_text_content_is_rejected(base_payload) -> None:
    base_payload["messages"][0]["content"] = [{"type": "text", "text": "hello"}]

    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(base_payload)
