import json

import pytest

from app.agent.graph import build_graph

from .conftest import base_state, collect_graph_events


def names(events):
    return [item["event"] for item in events]


def data(item):
    return json.loads(item["data"])


@pytest.mark.asyncio
async def test_graph_generates_outline(monkeypatch, sample_chunks):
    async def fake_retrieve(**kwargs):
        return sample_chunks

    monkeypatch.setattr("app.rag.stub.retrieve", fake_retrieve)
    events = await collect_graph_events(build_graph(), base_state(query="写份知识库报告"))

    assert "outline_delta" in names(events)
    complete = [data(item)["outline"] for item in events if item["event"] == "outline_complete"]
    assert complete[-1]["title"] == "示例报告"
    assert events[-2]["event"] == "references"


@pytest.mark.asyncio
async def test_graph_modifies_outline_without_retrieval(monkeypatch):
    async def fake_retrieve(**kwargs):
        raise AssertionError("outline_modify should not retrieve")

    monkeypatch.setattr("app.rag.stub.retrieve", fake_retrieve)
    events = await collect_graph_events(
        build_graph(),
        base_state(
            query="删掉第三章",
            current_outline={"title": "旧大纲", "sections": [{"index": 3, "title": "第三章"}]},
            outline_confirmed=False,
        ),
    )

    assert "outline_delta" in names(events)
    assert "outline_complete" in names(events)
    assert "references" not in names(events)


@pytest.mark.asyncio
async def test_graph_writes_report(monkeypatch, sample_chunks):
    async def fake_retrieve(**kwargs):
        return sample_chunks

    monkeypatch.setattr("app.rag.stub.retrieve", fake_retrieve)
    events = await collect_graph_events(
        build_graph(),
        base_state(
            query="开始写",
            current_outline={"title": "示例报告", "sections": []},
            outline_confirmed=True,
        ),
    )

    event_names = names(events)
    assert "report_start" in event_names
    assert "report_text_delta" in event_names
    assert event_names[-3:] == ["references", "report_end", "stream_end"]


@pytest.mark.asyncio
async def test_graph_edits_report(monkeypatch, sample_chunks):
    async def fake_retrieve(**kwargs):
        return sample_chunks

    monkeypatch.setattr("app.rag.stub.retrieve", fake_retrieve)
    events = await collect_graph_events(
        build_graph(),
        base_state(query="第二章改正式", current_report="# 原报告"),
    )

    event_names = names(events)
    assert "report_start" in event_names
    assert "report_text_delta" in event_names
    assert "report_end" in event_names
    assert "references" in event_names


@pytest.mark.asyncio
async def test_graph_runs_qa_before_outline_for_multi_intent(monkeypatch, mock_llm_provider, sample_chunks):
    async def fake_retrieve(**kwargs):
        return sample_chunks

    def response_for_multi_intent(messages):
        system_text = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        if "对话意图识别助手" in system_text or "当前用户已有" in system_text:
            return (
                '{"intents": ['
                '{"intent_type": "knowledge_qa", "confidence": 0.9},'
                '{"intent_type": "report_outline", "confidence": 0.9}'
                '], "reason": "qa and outline"}'
            )
        if "查询改写助手" in system_text:
            return (
                '{"queries": ['
                '{"id": "q1", "question": "介绍知识库内容", "retrieval_query": "知识库内容", '
                '"answer_focus": "先回答知识问题"}'
                '], "reason": "single qa"}'
            )
        if "文档路由助手" in system_text:
            return '{"selected_doc_ids": ["doc_a"], "reason": "mock"}'
        if "报告大纲设计助手" in system_text:
            return (
                "# 示例报告\n\n## 1. 背景\n\n"
                '```json\n{"title":"示例报告","sections":[{"index":1,"title":"背景","subsections":[]}]}\n```'
            )
        return "这是先给左侧对话的知识问答回答。\n<USED_CHUNKS>[\"ck_a\"]</USED_CHUNKS>"

    monkeypatch.setattr("app.rag.stub.retrieve", fake_retrieve)
    monkeypatch.setattr(mock_llm_provider, "_response_for", response_for_multi_intent)

    events = await collect_graph_events(build_graph(), base_state(query="先介绍知识库内容，再帮我生成报告大纲"))
    event_names = names(events)
    intent_payload = [data(item) for item in events if item["event"] == "intent"][-1]
    text = "".join(data(item)["delta"] for item in events if item["event"] == "text_delta")

    assert intent_payload == [
        {"intent_type": "knowledge_qa", "confidence": 0.9},
        {"intent_type": "report_outline", "confidence": 0.9},
    ]
    assert "知识问答回答" in text
    assert event_names.index("text_delta") < event_names.index("outline_delta")
    assert "outline_complete" in event_names


@pytest.mark.asyncio
async def test_graph_runs_qa_before_report_write_for_confirmed_outline_multi_intent(
    monkeypatch, mock_llm_provider, sample_chunks
):
    async def fake_retrieve(**kwargs):
        return sample_chunks

    def response_for_report_write_multi_intent(messages):
        system_text = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        if "查询改写助手" in system_text:
            return (
                '{"queries": ['
                '{"id": "q1", "question": "徐洪波的过往经历是什么", '
                '"retrieval_query": "徐洪波 过往经历 教育背景 工作经历", '
                '"answer_focus": "回答徐洪波的过往经历"}'
                '], "reason": "side qa"}'
            )
        if "文档路由助手" in system_text:
            return '{"selected_doc_ids": ["doc_a"], "reason": "mock"}'
        if "专业的报告撰写助手" in system_text:
            return "# 报告正文\n\n这是右侧报告内容。\n<USED_CHUNKS>[\"ck_a\"]</USED_CHUNKS>"
        return "徐洪波的过往经历包括教育背景和工作经历。\n<USED_CHUNKS>[\"ck_a\"]</USED_CHUNKS>"

    monkeypatch.setattr("app.rag.stub.retrieve", fake_retrieve)
    monkeypatch.setattr(mock_llm_provider, "_response_for", response_for_report_write_multi_intent)

    events = await collect_graph_events(
        build_graph(),
        base_state(
            query="OK,开始写吧。顺便回答一下徐洪波的过往经历",
            current_outline={"title": "示例报告", "sections": []},
            outline_confirmed=True,
        ),
    )
    event_names = names(events)
    intent_payload = [data(item) for item in events if item["event"] == "intent"][-1]
    chat_text = "".join(data(item)["delta"] for item in events if item["event"] == "text_delta")
    report_text = "".join(data(item)["delta"] for item in events if item["event"] == "report_text_delta")

    assert intent_payload == [
        {"intent_type": "knowledge_qa", "confidence": 0.9},
        {"intent_type": "report_write", "confidence": 0.95},
    ]
    assert "徐洪波" in chat_text
    assert "右侧报告内容" in report_text
    assert event_names.index("text_delta") < event_names.index("report_text_delta")


@pytest.mark.asyncio
async def test_report_write_prompt_filters_meta_outline_and_forbids_json_output(
    monkeypatch, mock_llm_provider, sample_chunks
):
    async def fake_retrieve(**kwargs):
        return sample_chunks

    original_response = mock_llm_provider._response_for

    def response_for_report_prompt_check(messages):
        system_text = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        user_text = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
        if "专业的报告撰写助手" not in system_text:
            return original_response(messages)

        assert "不要回显大纲" in system_text
        assert "逐字使用大纲中的报告标题和章节标题" in system_text
        assert "不要输出结构化JSON" in system_text
        assert "```json" not in user_text
        assert '"sections"' not in user_text
        assert "修改后的大纲" not in user_text
        assert "结构化JSON" not in user_text
        assert "报告标题：马飞研究员个人介绍报告" in user_text
        assert "一、报告摘要" in user_text
        assert "二、个人基本概况" in user_text
        assert "2.1 基础身份信息" in user_text
        assert "报告摘要" in user_text
        assert "个人基本概况" in user_text
        return "# 马飞研究员个人介绍报告\n\n## 一、报告摘要\n正文。\n<USED_CHUNKS>[\"ck_a\"]</USED_CHUNKS>"

    monkeypatch.setattr("app.rag.stub.retrieve", fake_retrieve)
    monkeypatch.setattr(mock_llm_provider, "_response_for", response_for_report_prompt_check)

    events = await collect_graph_events(
        build_graph(),
        base_state(
            query="开始写",
            current_outline={
                "title": "马飞研究员个人介绍报告",
                "sections": [
                    {"index": 1, "title": "第一部分：修改后的大纲", "subsections": []},
                    {"index": 2, "title": "一、报告摘要", "subsections": []},
                    {
                        "index": 3,
                        "title": "二、个人基本概况",
                        "subsections": [{"index": "2.1", "title": "基础身份信息"}],
                    },
                    {"index": 9, "title": "第二部分：结构化JSON", "subsections": []},
                ],
            },
            outline_confirmed=True,
        ),
    )
    report_text = "".join(data(item)["delta"] for item in events if item["event"] == "report_text_delta")

    assert "修改后的大纲" not in report_text
    assert "结构化JSON" not in report_text
    assert "```json" not in report_text
    assert "报告摘要" in report_text
