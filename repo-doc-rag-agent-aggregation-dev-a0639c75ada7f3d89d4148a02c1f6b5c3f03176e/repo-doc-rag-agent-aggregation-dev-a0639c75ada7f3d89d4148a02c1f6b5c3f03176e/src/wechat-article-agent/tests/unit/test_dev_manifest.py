from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev.server import load_manifest_documents


def test_manifest_loader_keeps_only_completed_documents(tmp_path: Path) -> None:
    path = tmp_path / "documents.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"doc_id": "doc-ready", "status": "completed"}),
                json.dumps({"doc_id": "doc-running", "status": "processing"}),
                json.dumps({"status": "completed"}),
            ]
        ),
        encoding="utf-8",
    )

    assert load_manifest_documents(path) == [{"doc_id": "doc-ready", "status": "completed"}]


def test_manifest_loader_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "documents.jsonl"
    path.write_text("not json\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON"):
        load_manifest_documents(path)
