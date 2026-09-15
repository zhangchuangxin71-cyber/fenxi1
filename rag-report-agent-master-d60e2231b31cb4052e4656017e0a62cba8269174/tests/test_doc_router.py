import pytest

from app.agent.nodes.doc_router import doc_router_node

from .conftest import base_state


@pytest.mark.asyncio
async def test_doc_router_selects_relevant_documents(monkeypatch):
    async def fake_load(kb_id, doc_ids):
        return {
            "doc_a": {"doc_name": "A.pdf", "doc_description": "相关"},
            "doc_b": {"doc_name": "B.pdf", "doc_description": "无关"},
        }

    monkeypatch.setattr("app.agent.nodes.doc_router.load_doc_meta_async", fake_load)
    state = base_state(doc_ids=["doc_a", "doc_b"], temp_doc_ids=[])

    result = await doc_router_node(state, {"configurable": {"emitter": None}})

    assert result["selected_doc_ids"] == ["doc_a"]
    assert result["selected_temp_doc_ids"] == []
