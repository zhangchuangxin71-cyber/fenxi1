import json
import re
from types import SimpleNamespace

import pytest

from app.agent.graph import build_graph
from app.agent.nodes.common import collect_stream
from app.api.sse import EventEmitter
from app.llm.base import LLMProvider
from app.llm.doubao import DoubaoProvider
from app.llm.registry import clear_providers, register

from .conftest import base_state, collect_graph_events


def _payload(item):
    return json.loads(item["data"])


@pytest.mark.asyncio
async def test_graph_qa_emits_thinking_delta_events(monkeypatch, sample_chunks):
    async def fake_retrieve(**kwargs):
        return sample_chunks

    monkeypatch.setattr("app.rag.stub.retrieve", fake_retrieve)

    events = await collect_graph_events(build_graph(), base_state(query="什么是以人为中心的世界模型？"))
    thinking = [_payload(item)["delta"] for item in events if item["event"] == "thinking_delta"]
    thinking_text = "".join(thinking)

    assert any("思考：" in delta for delta in thinking)
    assert "调用工具" not in thinking_text
    assert "PostgreSQL" not in thinking_text
    assert "doc_nodes" not in thinking_text
    assert "doc_pages" not in thinking_text
    assert "chunk" not in thinking_text.lower()
    assert "USED_CHUNKS" not in thinking_text
    assert not re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        thinking_text,
        flags=re.IGNORECASE,
    )


@pytest.mark.asyncio
async def test_doubao_stream_enables_thinking_and_yields_reasoning_delta():
    class FakeStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if hasattr(self, "_done"):
                raise StopAsyncIteration
            self._done = True
            delta = SimpleNamespace(content="", reasoning_content="思考：需要先理解问题。")
            return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)

    class FakeCompletions:
        def __init__(self):
            self.kwargs = None

        async def create(self, **kwargs):
            self.kwargs = kwargs
            return FakeStream()

    fake_completions = FakeCompletions()
    provider = DoubaoProvider()
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=fake_completions)
    )

    chunks = [
        item
        async for item in provider.chat_stream(
            [{"role": "user", "content": "hi"}],
            model="doubao-test",
        )
    ]

    assert fake_completions.kwargs["extra_body"]["thinking"]["type"] == "enabled"
    assert chunks == [{"delta": "", "reasoning_delta": "思考：需要先理解问题。", "usage": None}]


@pytest.mark.asyncio
async def test_collect_stream_suppresses_raw_reasoning_and_streams_content_without_used_marker():
    class TinyDeltaProvider(LLMProvider):
        async def chat(self, *args, **kwargs):
            raise AssertionError("chat should not be used")

        async def chat_stream(self, messages, *, model, temperature=0.0, max_tokens=4096):
            reasoning = "用户现在要求改写报告，需要先理解要求，再根据资料组织正式表达。"
            content = (
                "第一段内容用于验证正文不是等到最后才整体输出。"
                "第二段内容继续增长，应该被分成多个 SSE text_delta。"
                "第三段内容用于保证缓冲器有机会按标点切分。"
                "第四段继续补充足够长度，模拟真实报告生成时的大段正文。"
                "第五段继续补充足够长度，确保第一个 text_delta 能提前发出。"
                "第六段继续补充足够长度，确保后续正文仍然保持流式输出。"
                "第七段继续补充足够长度，避免测试只覆盖最终 flush 的路径。"
                "第八段继续补充足够长度，模拟领导汇报材料中的正式表述。"
                "第九段继续补充足够长度，验证标点边界附近的切分效果。"
                "<USED_CHUNKS>[\"ck_a\"]</USED_CHUNKS>"
            )
            for char in reasoning:
                yield {"reasoning_delta": char, "delta": "", "usage": None}
            for char in content:
                yield {"reasoning_delta": "", "delta": char, "usage": None}
            yield {"reasoning_delta": "", "delta": "", "usage": {"total_tokens": 10}}

    clear_providers()
    register("doubao", TinyDeltaProvider())
    emitter = EventEmitter()

    text, usage = await collect_stream(
        [{"role": "user", "content": "test"}],
        model="mock",
        temperature=0,
        max_tokens=100,
        emitter=emitter,
        content_event="text_delta",
    )
    await emitter.close()
    events = [item async for item in emitter.stream()]

    thinking = [_payload(item)["delta"] for item in events if item["event"] == "thinking_delta"]
    text_deltas = [_payload(item)["delta"] for item in events if item["event"] == "text_delta"]

    assert usage == {"total_tokens": 10}
    assert "<USED_CHUNKS>" in text
    assert thinking == []
    assert len(text_deltas) >= 2
    assert "<USED_CHUNKS>" not in "".join(text_deltas)
