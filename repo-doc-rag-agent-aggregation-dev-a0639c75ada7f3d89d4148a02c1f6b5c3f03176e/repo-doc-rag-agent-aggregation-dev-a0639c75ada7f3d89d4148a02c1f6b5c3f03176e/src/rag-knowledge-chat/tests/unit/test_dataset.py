import json

import pytest

from dev.dataset import generate_request


def test_generate_request_keeps_gold_and_samples_deterministic_noise(tmp_path) -> None:
    labels = tmp_path / "query_labels.jsonl"
    rows = [
        {"query_id": "q1", "query_text": "问题一", "doc_id": "gold"},
        {"query_id": "q2", "query_text": "问题二", "doc_id": "noise-a"},
        {"query_id": "q3", "query_text": "问题三", "doc_id": "noise-b"},
    ]
    labels.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

    first = generate_request(
        manifest_dir=tmp_path,
        query_id="q1",
        user_id="u",
        kb_id="k",
        noise_count=2,
        seed=7,
    )
    second = generate_request(
        manifest_dir=tmp_path,
        query_id="q1",
        user_id="u",
        kb_id="k",
        noise_count=2,
        seed=7,
    )

    assert first == second
    assert first["messages"][-1]["content"] == "问题一"
    assert first["rag"]["doc_ids"][0] == "gold"
    assert set(first["rag"]["doc_ids"][1:]) == {"noise-a", "noise-b"}
    assert first["rag"]["incremental_doc_ids"] == []


def test_generate_request_rejects_too_many_noise_docs(tmp_path) -> None:
    (tmp_path / "query_labels.jsonl").write_text(
        '{"query_id":"q1","query_text":"问题","doc_id":"gold"}\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="noise"):
        generate_request(
            manifest_dir=tmp_path,
            query_id="q1",
            user_id="u",
            kb_id="k",
            noise_count=1,
            seed=1,
        )
