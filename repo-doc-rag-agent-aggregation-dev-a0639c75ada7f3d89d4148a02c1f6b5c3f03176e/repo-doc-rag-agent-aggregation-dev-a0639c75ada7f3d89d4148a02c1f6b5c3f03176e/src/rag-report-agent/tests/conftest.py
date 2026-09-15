from collections.abc import AsyncIterator

import pytest

from app.api.sse import EventEmitter
from app.llm.base import LLMProvider
from app.llm.registry import clear_providers, register
from app.rag.types import ProvidedChunkInput


class MockLLMProvider(LLMProvider):
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[dict] = []

    async def chat(
        self,
        messages,
        *,
        model,
        temperature=0.0,
        max_tokens=4096,
        response_format=None,
    ) -> dict:
        if self.fail:
            raise RuntimeError("mock llm failure")
        text = self._response_for(messages)
        self.calls.append({"type": "chat", "messages": messages, "model": model})
        return {
            "text": text,
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "raw": None,
        }

    async def chat_stream(
        self,
        messages,
        *,
        model,
        temperature=0.0,
        max_tokens=4096,
    ) -> AsyncIterator[dict]:
        if self.fail:
            raise RuntimeError("mock llm failure")
        text = self._response_for(messages)
        self.calls.append({"type": "stream", "messages": messages, "model": model})
        midpoint = max(1, len(text) // 2)
        yield {"reasoning_delta": "模型思考：先定位参考资料，再组织回答。", "delta": "", "usage": None}
        for delta in (text[:midpoint], text[midpoint:]):
            if delta:
                yield {"delta": delta, "usage": None}
        yield {"delta": "", "usage": {"prompt_tokens": 1, "completion_tokens": 3, "total_tokens": 4}}

    def _response_for(self, messages) -> str:
        system_text = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        user_text = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
        if "文档路由助手" in system_text:
            return '{"selected_doc_ids": ["doc_a"], "reason": "mock"}'
        if "对话意图识别助手" in system_text or "当前用户已有" in system_text:
            if "你好" in user_text:
                return '{"intent": "chitchat", "reason": "greeting"}'
            if "写份" in user_text or "报告" in user_text:
                return '{"intent": "report_outline", "reason": "report"}'
            return '{"intent": "knowledge_qa", "reason": "qa"}'
        if "报告大纲设计助手" in system_text:
            return (
                "# 示例报告\n\n## 1. 背景\n### 1.1 来源\n\n"
                '```json\n{"title":"示例报告","sections":[{"index":1,"title":"背景",'
                '"subsections":[{"index":"1.1","title":"来源"}]}]}\n```'
            )
        if "大纲修改助手" in system_text:
            return (
                "# 修改后的报告\n\n## 1. 背景\n\n"
                '```json\n{"title":"修改后的报告","sections":[{"index":1,"title":"背景",'
                '"subsections":[]}]}\n```'
            )
        if "专业的报告撰写助手" in system_text:
            return "# 示例报告\n\n## 1. 背景\n报告正文。\n<USED_CHUNKS>[\"ck_a\"]</USED_CHUNKS>"
        if "报告编辑助手" in system_text:
            return "# 修改后报告\n\n第二章更正式。\n<USED_CHUNKS>[\"ck_a\"]</USED_CHUNKS>"
        if "闲聊" in system_text:
            return "你好，我是知识库问答助手。"
        return "这是基于知识库的回答。\n<USED_CHUNKS>[\"ck_a\"]</USED_CHUNKS>"


@pytest.fixture(autouse=True)
def mock_llm_provider():
    clear_providers()
    provider = MockLLMProvider()
    register("doubao", provider)
    yield provider
    clear_providers()


@pytest.fixture
def sample_chunks():
    return [
        ProvidedChunkInput(
            chunk_id="ck_a",
            document_id="doc_a",
            document_name="示例文档.pdf",
            path="1",
            content="示例文档说明了知识库问答和报告生成。",
        )
    ]


async def collect_graph_events(graph, state):
    emitter = EventEmitter()
    await emitter.emit_stream_start(state["conversation_id"], state["session_id"])
    final_state = await graph.ainvoke(
        state,
        config={"configurable": {"emitter": emitter}, "recursion_limit": 25},
    )
    await emitter.emit_stream_end(final_state.get("usage", {}))
    await emitter.close()
    return [item async for item in emitter.stream()]


def base_state(**overrides):
    state = {
        "session_id": "sess_1",
        "user_id": "user_1",
        "conversation_id": "conv_1",
        "kb_id": "kb_1",
        "query": "示例问题是什么？",
        "history": [],
        "doc_ids": ["doc_a", "doc_b"],
        "temp_doc_ids": [],
        "retrieval_top_k": 5,
        "retrieval_search_mode": "hybrid",
        "gen_temperature": 0.2,
        "gen_max_tokens": 1024,
        "current_outline": None,
        "outline_confirmed": False,
        "current_report": None,
    }
    state.update(overrides)
    return state
