from types import SimpleNamespace

import pytest

from core.document_tools import resolve_restricted_doc_ids


def test_resolve_restricted_doc_ids_accepts_display_name_stem():
    client = SimpleNamespace(
        documents={
            "doc-1": {
                "doc_name": "光明区光明街道旅游手册.md",
                "doc_description": "",
                "type": "md",
                "path": "app/data/光明区光明街道旅游手册.md",
                "line_count": 10,
            }
        }
    )

    resolved = resolve_restricted_doc_ids(client, "光明区光明街道旅游手册")

    assert resolved == {"doc-1"}


def test_resolve_restricted_doc_ids_accepts_duplicate_stem_matches():
    client = SimpleNamespace(
        documents={
            "doc-1": {
                "doc_name": "sample.md",
                "doc_description": "",
                "type": "md",
                "path": "app/data/sample.md",
                "line_count": 10,
            },
            "doc-2": {
                "doc_name": "sample.pdf",
                "doc_description": "",
                "type": "pdf",
                "path": "app/data/sample.pdf",
                "page_count": 5,
            },
        }
    )

    resolved = resolve_restricted_doc_ids(client, "sample")

    assert resolved == {"doc-1", "doc-2"}
