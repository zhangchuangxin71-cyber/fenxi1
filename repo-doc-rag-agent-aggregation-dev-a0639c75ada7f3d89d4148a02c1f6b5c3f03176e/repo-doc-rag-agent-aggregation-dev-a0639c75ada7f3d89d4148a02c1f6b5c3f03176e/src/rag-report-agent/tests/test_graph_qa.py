import json

import pytest

from app.agent.graph import build_graph
from app.agent.nodes.chitchat import AGENT_IDENTITY_REPLY
from app.rag.types import ProvidedChunkInput

from .conftest import base_state, collect_graph_events


def event_names(events):
    return [item["event"] for item in events]


def payload(item):
    return json.loads(item["data"])


@pytest.mark.asyncio
async def test_graph_qa_emits_expected_sequence(monkeypatch, sample_chunks):
    async def fake_retrieve(**kwargs):
        return sample_chunks

    monkeypatch.setattr("app.rag.stub.retrieve", fake_retrieve)
    events = await collect_graph_events(build_graph(), base_state())
    names = event_names(events)
    non_thinking_names = [name for name in names if name != "thinking_delta"]

    assert non_thinking_names[:6] == ["stream_start", "step", "intent", "step", "step", "step"]
    assert "text_delta" in names
    assert names[-2:] == ["references", "stream_end"]
    assert payload(events[-2])["references"][0]["doc_name"] == "示例文档.pdf"


@pytest.mark.asyncio
async def test_graph_rag_miss_emits_fallback_and_empty_references(monkeypatch):
    async def fake_retrieve(**kwargs):
        return []

    monkeypatch.setattr("app.rag.stub.retrieve", fake_retrieve)
    events = await collect_graph_events(build_graph(), base_state())
    text = "".join(payload(item)["delta"] for item in events if item["event"] == "text_delta")
    refs = [payload(item)["references"] for item in events if item["event"] == "references"]

    assert text.startswith("（抱歉，知识库中未找到直接相关的内容")
    assert refs[-1] == []


@pytest.mark.asyncio
async def test_graph_qa_uses_retrieved_chunks_when_model_omits_reference_marker(
    monkeypatch, mock_llm_provider, sample_chunks
):
    async def fake_retrieve(**kwargs):
        return sample_chunks

    def response_without_reference_marker(messages):
        system_text = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        if "文档路由助手" in system_text or "对话意图识别助手" in system_text:
            return original_response(messages)
        return "Alpha beta."

    original_response = mock_llm_provider._response_for
    monkeypatch.setattr("app.rag.stub.retrieve", fake_retrieve)
    monkeypatch.setattr(mock_llm_provider, "_response_for", response_without_reference_marker)

    events = await collect_graph_events(build_graph(), base_state())
    refs = [payload(item)["references"] for item in events if item["event"] == "references"]

    assert refs[-1] == [
        {"doc_id": "doc_a", "doc_name": "示例文档.pdf", "chunk_ids": ["ck_a"]},
    ]


@pytest.mark.asyncio
async def test_graph_chitchat_has_no_references(monkeypatch):
    async def fake_retrieve(**kwargs):
        raise AssertionError("chitchat should not retrieve")

    monkeypatch.setattr("app.rag.stub.retrieve", fake_retrieve)
    events = await collect_graph_events(build_graph(), base_state(query="你好", doc_ids=[]))
    names = event_names(events)

    assert "intent" in names
    assert "text_delta" in names
    assert "references" not in names


@pytest.mark.asyncio
async def test_graph_identity_question_uses_private_agent_identity(monkeypatch):
    async def fake_retrieve(**kwargs):
        raise AssertionError("identity chitchat should not retrieve")

    monkeypatch.setattr("app.rag.stub.retrieve", fake_retrieve)
    events = await collect_graph_events(build_graph(), base_state(query="你是谁", doc_ids=[]))
    text = "".join(payload(item)["delta"] for item in events if item["event"] == "text_delta")

    assert text == AGENT_IDENTITY_REPLY
    assert "豆包" not in text
    assert "references" not in event_names(events)


@pytest.mark.asyncio
async def test_graph_mixed_identity_and_knowledge_question_keeps_answering_substantive_parts(
    monkeypatch, mock_llm_provider
):
    retrieved_calls = []
    chunks = [
        ProvidedChunkInput(
            chunk_id="ck_ma",
            document_id="doc_ma",
            document_name="马博士资料.pdf",
            path="1",
            content="马博士是光明实验室媒体智能团队负责人。",
        ),
        ProvidedChunkInput(
            chunk_id="ck_hotel",
            document_id="doc_hotel",
            document_name="光明区旅游手册.docx",
            path="46",
            content="光明区有适合商务出行和旅游住宿的酒店。",
        ),
    ]

    async def fake_retrieve(**kwargs):
        retrieved_calls.append(kwargs)
        if "马博士" in kwargs["query"]:
            return [chunks[0]]
        if "酒店" in kwargs["query"]:
            return [chunks[1]]
        return chunks

    def response_for_mixed_question(messages):
        system_text = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        user_text = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
        if "查询改写助手" in system_text:
            return (
                '{"queries": ['
                '{"id": "q1", "question": "介绍一下马博士", "retrieval_query": "马博士 负责人 经历 成果", '
                '"answer_focus": "介绍马博士"},'
                '{"id": "q2", "question": "推荐光明区的酒店", "retrieval_query": "光明区 酒店 推荐 价格 地址", '
                '"answer_focus": "推荐光明区酒店"}'
                "], \"reason\": \"multi question\"}"
            )
        if "文档路由助手" in system_text:
            return '{"selected_doc_ids": ["doc_ma", "doc_hotel"], "reason": "multi topic"}'
        if "对话意图识别助手" in system_text or "当前用户已有" in system_text:
            return '{"intent": "knowledge_qa", "reason": "mixed qa"}'
        assert "子问题 q1：介绍一下马博士" in user_text
        assert "子问题 q2：推荐光明区的酒店" in user_text
        assert "ck_ma" in user_text
        assert "ck_hotel" in user_text
        return "马博士是光明实验室媒体智能团队负责人。光明区可优先考虑旅游手册中的商务酒店。"

    monkeypatch.setattr("app.rag.stub.retrieve", fake_retrieve)
    monkeypatch.setattr(mock_llm_provider, "_response_for", response_for_mixed_question)

    events = await collect_graph_events(
        build_graph(),
        base_state(
            query="你是谁？可以帮我介绍一下马博士吗，然后给我推荐一下光明区的酒店",
            doc_ids=["doc_ma", "doc_hotel"],
        ),
    )
    text = "".join(payload(item)["delta"] for item in events if item["event"] == "text_delta")
    refs = [payload(item)["references"] for item in events if item["event"] == "references"]

    assert text.startswith(AGENT_IDENTITY_REPLY)
    assert "马博士" in text
    assert "酒店" in text
    assert [call["query"] for call in retrieved_calls] == [
        "马博士 负责人 经历 成果",
        "光明区 酒店 推荐 价格 地址",
    ]
    assert all(call["doc_ids"] == ["doc_ma", "doc_hotel"] for call in retrieved_calls)
    assert refs[-1] == [
        {"doc_id": "doc_ma", "doc_name": "马博士资料.pdf", "chunk_ids": ["ck_ma"]},
        {"doc_id": "doc_hotel", "doc_name": "光明区旅游手册.docx", "chunk_ids": ["ck_hotel"]},
    ]


@pytest.mark.asyncio
async def test_graph_rewrites_follow_up_question_with_history(monkeypatch, mock_llm_provider):
    retrieved_calls = []
    chunk = ProvidedChunkInput(
        chunk_id="ck_ma",
        document_id="doc_ma",
        document_name="马博士资料.pdf",
        path="1",
        content="马博士的科研成果包括多模态大模型和情感智能相关论文与专利。",
    )

    async def fake_retrieve(**kwargs):
        retrieved_calls.append(kwargs)
        return [chunk]

    def response_for_follow_up(messages):
        system_text = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        if "查询改写助手" in system_text:
            return (
                '{"queries": ['
                '{"id": "q1", "question": "马博士有哪些成果", '
                '"retrieval_query": "马博士 科研成果 论文 专利 多模态大模型", '
                '"answer_focus": "结合历史中的马博士主题介绍成果"}'
                "], \"reason\": \"history rewrite\"}"
            )
        if "文档路由助手" in system_text:
            return '{"selected_doc_ids": ["doc_ma"], "reason": "history"}'
        if "对话意图识别助手" in system_text or "当前用户已有" in system_text:
            return '{"intent": "knowledge_qa", "reason": "follow up"}'
        return "马博士的成果包括多模态大模型、情感智能等方向的论文与专利。"

    monkeypatch.setattr("app.rag.stub.retrieve", fake_retrieve)
    monkeypatch.setattr(mock_llm_provider, "_response_for", response_for_follow_up)

    events = await collect_graph_events(
        build_graph(),
        base_state(
            query="那他的成果呢？",
            doc_ids=["doc_ma"],
            history=[{"role": "user", "content": "马博士是谁？"}, {"role": "assistant", "content": "马博士是团队负责人。"}],
        ),
    )
    text = "".join(payload(item)["delta"] for item in events if item["event"] == "text_delta")

    assert retrieved_calls[0]["query"] == "马博士 科研成果 论文 专利 多模态大模型"
    assert "马博士" in text
    assert "成果" in text
