import asyncio

import pytest

from app.api.contracts import ChatCompletionRequest
from app.chat.events import (
    CompleteEvent,
    ErrorEvent,
    ReasoningDeltaEvent,
    StatusEvent,
    TextDeltaEvent,
)
from app.chat.models import (
    AnswerBasis,
    RetrievalResult,
    RetrievalStatus,
    RouteDecision,
    RouteQuery,
)
from app.chat.orchestrator import ChatOrchestrator
from app.integrations.ark import AnswerReasoningDelta, AnswerTextDelta, AnswerThinkingStarted
from app.platform.trace import TraceCollector
from tests.conftest import AllowLimiter, FakeArk, FakeRetrieval


async def collect(orchestrator, request):
    return [
        event
        async for event in orchestrator.run(
            request=request,
            request_id="req-1",
            trace=TraceCollector(enabled=request.rag.include_debug),
        )
    ]


@pytest.mark.asyncio
async def test_general_answer_uses_two_llm_calls_at_most_and_no_retrieval(base_payload) -> None:
    request = ChatCompletionRequest.model_validate(base_payload)
    ark = FakeArk(
        RouteDecision(needs_retrieval=False, query="", reason_code="identity"),
        deltas=[AnswerTextDelta(delta="我是由广州日报粤传媒和光明实验室联合研发的知识库问答助手。")],
    )
    retrieval = FakeRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT))
    orchestrator = ChatOrchestrator(ark=ark, retrieval=retrieval, ark_limiter=AllowLimiter())

    events = await collect(orchestrator, request)

    text = "".join(event.delta for event in events if isinstance(event, TextDeltaEvent))
    complete = next(event for event in events if isinstance(event, CompleteEvent))
    assert "本次回答未检索知识库" in text
    assert "广州日报粤传媒和光明实验室" in text
    assert len(ark.decision_calls) == 1
    assert len(ark.answer_calls) == 1
    assert ark.answer_calls[0]["temperature"] == request.temperature
    assert ark.answer_calls[0]["max_tokens"] == request.max_tokens
    assert retrieval.calls == []
    assert complete.answer_basis is AnswerBasis.GENERAL_NO_RETRIEVAL


@pytest.mark.asyncio
async def test_unresolved_reference_requests_clarification_without_retrieval_or_general_notice(
    base_payload,
) -> None:
    base_payload["messages"] = [
        {"role": "user", "content": "介绍一下报销制度"},
        {"role": "assistant", "content": "你想查看哪份制度？"},
        {"role": "user", "content": "那这篇文档的报销流程是什么？"},
    ]
    request = ChatCompletionRequest.model_validate(base_payload)
    ark = FakeArk(
        RouteDecision(needs_retrieval=False, query="", reason_code="clarification"),
        deltas=[AnswerTextDelta(delta="请问‘这篇文档’具体指哪一篇？")],
    )
    retrieval = FakeRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT))
    orchestrator = ChatOrchestrator(ark=ark, retrieval=retrieval, ark_limiter=AllowLimiter())

    events = await collect(orchestrator, request)

    text = "".join(event.delta for event in events if isinstance(event, TextDeltaEvent))
    assert retrieval.calls == []
    assert "请问‘这篇文档’具体指哪一篇？" in text
    assert "本次回答未检索知识库" not in text
    answer_system = ark.answer_calls[0]["messages"][0]["content"]
    assert "只提出一个简洁的澄清问题" in answer_system


@pytest.mark.asyncio
async def test_retrieval_success_generates_grounded_answer_with_references(base_payload) -> None:
    base_payload["rag"]["include_debug"] = True
    request = ChatCompletionRequest.model_validate(base_payload)
    ark = FakeArk(
        RouteDecision(needs_retrieval=True, query="审批流程", reason_code="knowledge_base"),
        deltas=[AnswerTextDelta(delta="需要两级确认[1]。")],
    )
    retrieval = FakeRetrieval(
        RetrievalResult(
            status=RetrievalStatus.SUCCESS,
            chunks=[
                {
                    "chunk_id": "c1",
                    "document_id": "doc-1",
                    "document_name": "制度.pdf",
                    "page_number": 3,
                    "path": "1",
                    "content": "审批需要两级确认。",
                    "score": 0.9,
                    "source_type": "node",
                }
            ],
            request_id="retr-1",
            debug={"trace": [{"stage": "route"}]},
        )
    )
    orchestrator = ChatOrchestrator(ark=ark, retrieval=retrieval, ark_limiter=AllowLimiter())

    events = await collect(orchestrator, request)

    assert any(isinstance(event, StatusEvent) and event.stage == "retrieval" for event in events)
    complete = next(event for event in events if isinstance(event, CompleteEvent))
    assert complete.answer_basis is AnswerBasis.KNOWLEDGE_BASE
    assert complete.references == [
        {
            "citation_index": 1,
            "doc_id": "doc-1",
            "doc_name": "制度.pdf",
            "page_number": 3,
        }
    ]
    assert complete.debug is not None
    stages = [item["stage"] for item in complete.debug["trace"]]
    assert "first_token" in stages
    assert retrieval.calls[0]["query"] == ["审批流程"]
    assert "当前用户请求了 1 篇文档" in ark.decision_calls[0][-1]["content"]
    assert "用户的原始问题为：文档里的审批流程是什么？" in ark.decision_calls[0][-1]["content"]
    assert len(ark.decision_calls) + len(ark.answer_calls) == 2


@pytest.mark.asyncio
async def test_preprocessed_query_list_is_forwarded_to_retrieval(base_payload) -> None:
    request = ChatCompletionRequest.model_validate(base_payload)
    ark = FakeArk(
        RouteDecision(
            needs_retrieval=True,
            query=["你能看到哪些文档？", "A 公司营业额是多少？"],
            reason_code="knowledge_base",
        )
    )
    retrieval = FakeRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT))

    await collect(ChatOrchestrator(ark=ark, retrieval=retrieval, ark_limiter=AllowLimiter()), request)

    assert retrieval.calls[0]["query"] == ["你能看到哪些文档？", "A 公司营业额是多少？"]


@pytest.mark.asyncio
async def test_mixed_resolution_retrieves_only_resolved_queries_and_prompts_for_the_rest(
    base_payload,
) -> None:
    request = ChatCompletionRequest.model_validate(base_payload)
    ark = FakeArk(
        RouteDecision(
            needs_retrieval=True,
            queries=[
                RouteQuery(
                    status="resolved",
                    reason="历史明确提到 A 公司年报。",
                    rewrite_query="A 公司年报中的营业额是多少？",
                ),
                RouteQuery(
                    status="ambiguous",
                    reason="历史中存在多篇候选文档。",
                    rewrite_query="这篇文档的审批流程是什么？",
                ),
            ],
            reason_code="knowledge_base",
        ),
        deltas=[AnswerTextDelta(delta="营业额为一亿元[1]。请说明审批流程对应的文件名。")],
    )
    retrieval = FakeRetrieval(
        RetrievalResult(
            status=RetrievalStatus.SUCCESS,
            chunks=[
                {
                    "chunk_id": "c1",
                    "document_id": "doc-1",
                    "document_name": "A公司年报.pdf",
                    "page_number": 2,
                    "content": "营业额为一亿元。",
                }
            ],
        )
    )

    events = await collect(
        ChatOrchestrator(ark=ark, retrieval=retrieval, ark_limiter=AllowLimiter()), request
    )

    assert retrieval.calls[0]["query"] == ["A 公司年报中的营业额是多少？"]
    answer_system = ark.answer_calls[0]["messages"][0]["content"]
    assert "关于这篇文档的审批流程是什么？问题，系统未能判断具体指代" in answer_system
    assert "不要为这个问题生成引用编号" in answer_system
    complete = next(event for event in events if isinstance(event, CompleteEvent))
    assert len(complete.chunks) == 1
    assert complete.references == [
        {
            "citation_index": 1,
            "doc_id": "doc-1",
            "doc_name": "A公司年报.pdf",
            "page_number": 2,
        }
    ]


@pytest.mark.asyncio
async def test_all_ambiguous_queries_skip_retrieval_and_create_no_pseudo_reference(
    base_payload,
) -> None:
    request = ChatCompletionRequest.model_validate(base_payload)
    ark = FakeArk(
        RouteDecision(
            needs_retrieval=True,
            queries=[
                RouteQuery(
                    status="ambiguous",
                    reason="无法确定文档。",
                    rewrite_query="这篇文档主要讲了什么？",
                )
            ],
            reason_code="knowledge_base",
        ),
        deltas=[AnswerTextDelta(delta="请告知具体文件名。")],
    )
    retrieval = FakeRetrieval(RetrievalResult(status=RetrievalStatus.SUCCESS))

    events = await collect(
        ChatOrchestrator(ark=ark, retrieval=retrieval, ark_limiter=AllowLimiter()), request
    )

    assert retrieval.calls == []
    answer_system = ark.answer_calls[0]["messages"][0]["content"]
    assert "只提出一个简洁的澄清问题" in answer_system
    assert "这篇文档主要讲了什么？" in answer_system
    complete = next(event for event in events if isinstance(event, CompleteEvent))
    assert complete.retrieval["status"] == "not_called"
    assert complete.chunks == []
    assert complete.references == []


@pytest.mark.asyncio
async def test_resolved_query_with_unresolved_marker_is_downgraded_before_retrieval(
    base_payload,
) -> None:
    request = ChatCompletionRequest.model_validate(base_payload)
    ark = FakeArk(
        RouteDecision(
            needs_retrieval=True,
            queries=[
                RouteQuery(
                    status="resolved",
                    reason="错误地认为已经消解。",
                    rewrite_query="上一篇文档的营业额是多少？",
                )
            ],
            reason_code="knowledge_base",
        ),
        deltas=[AnswerTextDelta(delta="请告知上一篇文档的文件名。")],
    )
    retrieval = FakeRetrieval(RetrievalResult(status=RetrievalStatus.SUCCESS))

    events = await collect(
        ChatOrchestrator(ark=ark, retrieval=retrieval, ark_limiter=AllowLimiter()), request
    )

    assert retrieval.calls == []
    complete = next(event for event in events if isinstance(event, CompleteEvent))
    assert complete.debug is None
    assert complete.references == []


@pytest.mark.asyncio
async def test_incremental_document_names_are_resolved_before_route_decision(base_payload) -> None:
    base_payload["messages"][0]["content"] = "那另外新增的文档讲了什么？"
    base_payload["rag"].update(
        {
            "session_id": "session-1",
            "doc_ids": ["doc-1", "doc-2"],
            "temp_doc_ids": ["temp-1"],
            "incremental_doc_ids": ["temp-1", "doc-2"],
        }
    )
    request = ChatCompletionRequest.model_validate(base_payload)
    ark = FakeArk(
        RouteDecision(
            needs_retrieval=True,
            queries=[
                RouteQuery(
                    status="resolved",
                    reason="增量文档名已由当前请求信息明确提供。",
                    rewrite_query="查看以下文档的名称与元信息：《临时补充.pdf》、《新增制度.pdf》",
                )
            ],
            reason_code="knowledge_base",
        )
    )
    retrieval = FakeRetrieval(
        RetrievalResult(status=RetrievalStatus.NO_RESULT),
        document_names_by_id={"temp-1": "临时补充.pdf", "doc-2": "新增制度.pdf"},
    )

    await collect(ChatOrchestrator(ark=ark, retrieval=retrieval, ark_limiter=AllowLimiter()), request)

    assert retrieval.meta_calls == [
        {
            "user_id": "user-1",
            "kb_id": "kb-1",
            "requested_doc_ids": ["temp-1", "doc-2"],
            "permanent_scope_ids": ["doc-1", "doc-2"],
            "temporary_scope_ids": ["temp-1"],
            "session_id": "session-1",
        }
    ]
    assert "临时补充.pdf" in ark.decision_calls[0][-1]["content"]
    assert "新增制度.pdf" in ark.decision_calls[0][-1]["content"]
    assert "doc-2" not in ark.decision_calls[0][-1]["content"]
    assert retrieval.calls[0]["query"] == ["查看以下文档的名称与元信息：《临时补充.pdf》、《新增制度.pdf》"]


@pytest.mark.asyncio
async def test_unique_incremental_document_guard_recovers_current_document_reference(
    base_payload,
) -> None:
    base_payload["rag"].update(
        {
            "doc_ids": ["doc-1", "doc-2"],
            "incremental_doc_ids": ["doc-2"],
        }
    )
    request = ChatCompletionRequest.model_validate(base_payload)
    ark = FakeArk(
        RouteDecision(
            needs_retrieval=True,
            queries=[
                RouteQuery(
                    status="ambiguous",
                    reason="模型没有完成当前文档指代消解。",
                    rewrite_query="这个文件的营业额是多少？",
                )
            ],
            reason_code="knowledge_base",
        )
    )
    retrieval = FakeRetrieval(
        RetrievalResult(status=RetrievalStatus.NO_RESULT),
        document_names_by_id={"doc-2": "酒鬼酒2023年半年度报告.pdf"},
    )

    await collect(ChatOrchestrator(ark=ark, retrieval=retrieval, ark_limiter=AllowLimiter()), request)

    assert retrieval.calls[0]["query"] == ["《酒鬼酒2023年半年度报告.pdf》的营业额是多少？"]


@pytest.mark.asyncio
async def test_answer_receives_incremental_document_context_and_resolved_queries(
    base_payload,
) -> None:
    base_payload["rag"].update(
        {
            "doc_ids": ["doc-1", "doc-2"],
            "incremental_doc_ids": ["doc-2"],
        }
    )
    request = ChatCompletionRequest.model_validate(base_payload)
    ark = FakeArk(
        RouteDecision(
            needs_retrieval=True,
            queries=[
                RouteQuery(
                    status="resolved",
                    reason="本轮增量文档和历史文档均已确定。",
                    rewrite_query="酒鬼酒年报与泉阳泉年报的营业额分别是多少？",
                )
            ],
            reason_code="knowledge_base",
        )
    )
    retrieval = FakeRetrieval(
        RetrievalResult(
            status=RetrievalStatus.SUCCESS,
            chunks=[
                {
                    "chunk_id": "c1",
                    "document_id": "doc-2",
                    "document_name": "酒鬼酒2023年半年度报告.pdf",
                    "page_number": 1,
                    "content": "营业收入为15亿元。",
                }
            ],
        ),
        document_names_by_id={"doc-2": "酒鬼酒2023年半年度报告.pdf"},
    )

    await collect(ChatOrchestrator(ark=ark, retrieval=retrieval, ark_limiter=AllowLimiter()), request)

    answer_system = ark.answer_calls[0]["messages"][0]["content"]
    assert "当前用户请求范围内共有 2 篇文档" in answer_system
    assert "酒鬼酒2023年半年度报告.pdf" in answer_system
    assert "酒鬼酒年报与泉阳泉年报的营业额分别是多少" in answer_system


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "basis", "notice"),
    [
        (RetrievalStatus.NO_RESULT, AnswerBasis.GENERAL_NO_RESULT, "未检索到相关内容"),
        (RetrievalStatus.ERROR, AnswerBasis.GENERAL_RETRIEVAL_ERROR, "检索暂时不可用"),
    ],
)
async def test_retrieval_failure_modes_fall_back_to_general_answer(
    base_payload, status, basis, notice
) -> None:
    request = ChatCompletionRequest.model_validate(base_payload)
    ark = FakeArk(
        RouteDecision(needs_retrieval=True, query="问题", reason_code="knowledge_base"),
        deltas=[AnswerTextDelta(delta="通识回答")],
    )
    retrieval = FakeRetrieval(
        RetrievalResult(status=status, error={"status_code": 401, "code": "unauthorized"})
    )
    orchestrator = ChatOrchestrator(ark=ark, retrieval=retrieval, ark_limiter=AllowLimiter())

    events = await collect(orchestrator, request)

    text = "".join(event.delta for event in events if isinstance(event, TextDeltaEvent))
    complete = next(event for event in events if isinstance(event, CompleteEvent))
    assert notice in text
    assert complete.answer_basis is basis


@pytest.mark.asyncio
async def test_retrieval_422_uses_standard_retrieval_error_fallback(
    base_payload,
) -> None:
    request = ChatCompletionRequest.model_validate(base_payload)
    ark = FakeArk(
        RouteDecision(needs_retrieval=True, query="问题", reason_code="knowledge_base"),
        deltas=[AnswerTextDelta(delta="不应生成")],
    )
    retrieval = FakeRetrieval(
        RetrievalResult(
            status=RetrievalStatus.ERROR,
            error={
                "code": "retrieval_http_error",
                "status_code": 422,
                "retryable": False,
            },
        )
    )

    events = await collect(
        ChatOrchestrator(ark=ark, retrieval=retrieval, ark_limiter=AllowLimiter()), request
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    complete = next(event for event in events if isinstance(event, CompleteEvent))
    assert complete.answer_basis is AnswerBasis.GENERAL_RETRIEVAL_ERROR
    assert len(ark.answer_calls) == 1


@pytest.mark.asyncio
async def test_reasoning_emits_one_thinking_status_before_visible_text(base_payload) -> None:
    base_payload["rag"]["include_debug"] = True
    request = ChatCompletionRequest.model_validate(base_payload)
    ark = FakeArk(
        RouteDecision(needs_retrieval=False, query="", reason_code="chitchat"),
        deltas=[
            AnswerThinkingStarted(),
            AnswerReasoningDelta(delta="内部推理"),
            AnswerTextDelta(delta="可见正文"),
        ],
    )
    orchestrator = ChatOrchestrator(
        ark=ark,
        retrieval=FakeRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT)),
        ark_limiter=AllowLimiter(),
    )

    events = await collect(orchestrator, request)

    thinking_indexes = [
        index
        for index, event in enumerate(events)
        if isinstance(event, StatusEvent) and event.stage == "thinking"
    ]
    text_indexes = [index for index, event in enumerate(events) if isinstance(event, TextDeltaEvent)]
    model_text_index = next(index for index in text_indexes if events[index].delta == "可见正文")
    reasoning_indexes = [
        index for index, event in enumerate(events) if isinstance(event, ReasoningDeltaEvent)
    ]
    assert len(thinking_indexes) == 1
    assert thinking_indexes[0] < reasoning_indexes[0] < text_indexes[0]
    assert text_indexes[0] < model_text_index
    assert events[text_indexes[0]].delta.startswith("本次回答未检索知识库")
    assert events[thinking_indexes[0]].message == "正在思考"
    reasoning = [event for event in events if isinstance(event, ReasoningDeltaEvent)]
    assert [event.delta for event in reasoning] == ["内部推理"]
    complete = next(event for event in events if isinstance(event, CompleteEvent))
    stages = [item["stage"] for item in complete.debug["trace"]]
    assert stages.index("thinking_started") < stages.index("first_token")


@pytest.mark.asyncio
async def test_ark_error_after_stream_start_becomes_in_band_error(base_payload) -> None:
    base_payload["rag"]["include_debug"] = True
    request = ChatCompletionRequest.model_validate(base_payload)
    ark = FakeArk(
        RouteDecision(needs_retrieval=False, query="", reason_code="general_knowledge"),
        fail_stream=RuntimeError("secret upstream details"),
    )
    orchestrator = ChatOrchestrator(
        ark=ark,
        retrieval=FakeRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT)),
        ark_limiter=AllowLimiter(),
    )

    events = await collect(orchestrator, request)

    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert error.code == "ark_upstream_error"
    assert "secret" not in error.message
    assert error.debug is not None
    assert error.debug["trace"][-1]["stage"] == "generation_failed"


@pytest.mark.asyncio
async def test_total_deadline_becomes_in_band_error(base_payload) -> None:
    request = ChatCompletionRequest.model_validate(base_payload)

    class SlowArk(FakeArk):
        async def decide(self, messages):
            await asyncio.sleep(0.05)
            return await super().decide(messages)

    orchestrator = ChatOrchestrator(
        ark=SlowArk(RouteDecision(needs_retrieval=False, query="", reason_code="identity")),
        retrieval=FakeRetrieval(RetrievalResult(status=RetrievalStatus.NO_RESULT)),
        ark_limiter=AllowLimiter(),
        deadline_seconds=0.001,
    )

    events = await collect(orchestrator, request)

    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert error.code == "request_deadline_exceeded"
