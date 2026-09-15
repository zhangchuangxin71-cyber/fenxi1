from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class WorkspaceStore(ABC):
    """Storage abstraction for workspace metadata and full documents."""

    @abstractmethod
    def load_meta(self) -> dict[str, dict[str, Any]]:
        """Return lightweight metadata mapping: doc_id -> meta dict."""

    @abstractmethod
    def save_doc(self, doc_id: str, doc: dict[str, Any]) -> None:
        """Persist a full document payload."""

    @abstractmethod
    def load_full_doc(self, doc_id: str) -> dict[str, Any] | None:
        """Load a full document payload by id."""

    @abstractmethod
    def delete_doc(self, doc_id: str) -> None:
        """Delete one document payload by id."""


def make_meta_entry(doc: dict[str, Any]) -> dict[str, Any]:
    """Build a lightweight meta entry from a full document dict."""
    entry = {
        "type": doc.get("type", ""),
        "doc_name": doc.get("doc_name", ""),
        "doc_description": doc.get("doc_description", ""),
        "path": doc.get("path", ""),
    }
    if doc.get("session_id"):
        entry["session_id"] = doc.get("session_id")
    if doc.get("is_temporary") is not None:
        entry["is_temporary"] = bool(doc.get("is_temporary"))
    if doc.get("type") == "pdf":
        entry["page_count"] = doc.get("page_count")
    elif doc.get("type") == "md":
        entry["line_count"] = doc.get("line_count")
    return entry
