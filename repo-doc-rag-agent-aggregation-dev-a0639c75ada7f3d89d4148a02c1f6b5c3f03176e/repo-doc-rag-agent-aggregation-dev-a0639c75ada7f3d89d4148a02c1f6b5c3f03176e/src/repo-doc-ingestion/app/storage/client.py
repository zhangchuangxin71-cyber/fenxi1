from __future__ import annotations

"""Minimal PageIndex-compatible storage client for ingestion.

The standalone ingestion service only needs to persist and lazy-load parsed
document payloads. QA/retrieval helpers from the original PageIndex package are
intentionally not included here.
"""

import os
from pathlib import Path
from typing import Any
from .postgres_store import PostgresWorkspaceStore


class PageIndexClient:
    """Small persistence client used by the ingestion worker."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        retrieve_model: str | None = None,
        workspace: str | None = None,
        storage_backend: str | None = None,
        postgres_dsn: str | None = None,
        storage_fallback_to_file: bool = False,
        postgres_auto_init_tables: bool = True,
        user_id: str | None = None,
        kb_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        if api_key:
            os.environ["ARK_API_KEY"] = api_key
        self.workspace = Path(workspace).expanduser() if workspace else None
        if self.workspace:
            self.workspace.mkdir(parents=True, exist_ok=True)
        self.model = model or os.getenv("ARK_MODEL") or os.getenv("OPENAI_MODEL") or ""
        self.retrieve_model = retrieve_model or os.getenv("ARK_RETRIEVE_MODEL") or self.model
        backend = (storage_backend or os.getenv("PAGEINDEX_STORAGE_BACKEND") or "postgres").strip().lower()
        if backend != "postgres":
            raise ValueError("repo-doc-ingestion only supports postgres storage backend")
        dsn = postgres_dsn or os.getenv("POSTGRES_DSN") or os.getenv("DA_POSTGRES_DSN")
        self.storage_backend = "postgres"
        self._store = PostgresWorkspaceStore(
            dsn=dsn,
            auto_init_tables=postgres_auto_init_tables,
            user_id=user_id,
            kb_id=kb_id,
            session_id=session_id,
        )
        self.documents: dict[str, dict[str, Any]] = {}
        self._load_workspace()

    def _save_doc(self, doc_id: str) -> None:
        doc = dict(self.documents[doc_id])
        self._store.save_doc(doc_id, doc)
        self.documents[doc_id].pop("structure", None)
        self.documents[doc_id].pop("pages", None)

    def _load_workspace(self) -> None:
        meta = self._store.load_meta()
        for doc_id, entry in meta.items():
            self.documents[str(doc_id)] = dict(entry, id=str(doc_id), doc_id=str(doc_id))

    def load_full_doc(self, doc_id: str) -> dict[str, Any] | None:
        return self._store.load_full_doc(str(doc_id))

    def delete_doc(self, doc_id: str) -> None:
        self._store.delete_doc(str(doc_id))
        self.documents.pop(str(doc_id), None)

