"""Unified video retrieval: Chinese-CLIP (visual) + hybrid BGE (text)."""

from .config import PACKAGE_ROOT, WORKSPACE_ROOT
from .profile_paths import ProfileLayout, resolve_profile_layout

__all__ = [
    "PACKAGE_ROOT",
    "WORKSPACE_ROOT",
    "ProfileLayout",
    "resolve_profile_layout",
]
