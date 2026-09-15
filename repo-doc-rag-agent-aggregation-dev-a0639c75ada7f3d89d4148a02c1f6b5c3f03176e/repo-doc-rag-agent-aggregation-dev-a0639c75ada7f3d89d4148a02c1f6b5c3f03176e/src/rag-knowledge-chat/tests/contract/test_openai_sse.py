import json

from app.chat.events import (
    CompleteEvent,
    ErrorEvent,
    ReasoningDeltaEvent,
    StatusEvent,
    TextDeltaEvent,
)
from app.chat.models import AnswerBasis
from app.streaming.openai_sse import StreamContext, encode_event


def payload(event):
    encoded = encode_event(
        event,
        StreamContext(
            completion_id="chatcmpl_test",
            created=123,
            model="rag-knowledge-chat",
        ),
    )
    assert encoded.startswith("data: ")
    return json.loads(encoded.removeprefix("data: ").strip())


def test_status_and_text_are_standard_chunks_with_rag_extension() -> None:
    status = payload(StatusEvent(stage="retrieval", message="正在检索知识库"))
    text = payload(TextDeltaEvent(delta="正文"))

    assert status["object"] == "chat.completion.chunk"
    assert status["choices"][0]["delta"] == {}
    assert status["rag"]["event"]["stage"] == "retrieval"
    assert text["choices"][0]["delta"]["content"] == "正文"


def test_reasoning_uses_provider_compatible_delta_field() -> None:
    reasoning = payload(ReasoningDeltaEvent(delta="分析过程"))

    assert reasoning["object"] == "chat.completion.chunk"
    assert reasoning["choices"][0]["delta"] == {"reasoning_content": "分析过程"}
    assert "rag" not in reasoning


def test_complete_chunk_has_metadata_and_standard_finish_reason() -> None:
    complete = payload(
        CompleteEvent(
            answer_basis=AnswerBasis.GENERAL_NO_RESULT,
            retrieval={"status": "no_result"},
        )
    )

    assert complete["choices"][0]["finish_reason"] == "stop"
    assert complete["rag"]["answer_basis"] == "general_no_result"


def test_error_is_only_in_rag_extension() -> None:
    error = payload(
        ErrorEvent(
            code="ark_upstream_error",
            message="暂时不可用",
            retryable=True,
            debug={"trace": [{"stage": "generation_failed"}]},
        )
    )

    assert error["choices"][0]["finish_reason"] is None
    assert error["rag"]["error"]["code"] == "ark_upstream_error"
    assert "details" not in error["rag"]["error"]
    assert error["rag"]["debug"]["trace"][0]["stage"] == "generation_failed"


def test_error_event_contract_has_no_upstream_validation_details() -> None:
    assert "details" not in ErrorEvent.__dataclass_fields__
