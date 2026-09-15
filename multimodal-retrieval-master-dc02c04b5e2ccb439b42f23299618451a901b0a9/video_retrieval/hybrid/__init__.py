"""Hybrid video retrieval (BGE dense + SQLite FTS sparse)."""

from __future__ import annotations

from .hybrid_retrieval import HybridSearchConfig, HybridSearchEngine, build_search_engine

__all__ = [
    "HybridSearchConfig",
    "HybridSearchEngine",
    "build_search_engine",
]
