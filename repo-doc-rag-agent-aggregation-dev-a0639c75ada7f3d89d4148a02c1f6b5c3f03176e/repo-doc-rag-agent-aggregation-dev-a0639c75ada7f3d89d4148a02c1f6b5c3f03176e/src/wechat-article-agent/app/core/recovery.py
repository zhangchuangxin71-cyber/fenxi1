from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

RecoveryAction = Literal["resume_checkpoint", "new_revision", "needs_user_fix"]

_NEW_REVISION_CODES = {
    "ARK_STRUCTURED_OUTPUT_INVALID",
    "GENERATION_ATTEMPTS_EXHAUSTED",
    "ARTICLE_RETRIEVAL_ROUNDS_EXHAUSTED",
    "INSUFFICIENT_REQUIREMENTS",
}

_TRANSIENT_CODES = {
    "AGENT_RUNTIME_FAILED",
    "ARK_NETWORK_ERROR",
    "ARK_QUEUE_FULL",
    "ARK_QUEUE_TIMEOUT",
    "ARK_RESPONSE_FAILED",
    "ARK_STREAM_TIMEOUT",
    "ARK_UPSTREAM_ERROR",
    "DATABASE_UNAVAILABLE",
    "RETRIEVAL_UNAVAILABLE",
    "SEEDREAM_UNAVAILABLE",
    "STREAM_RUNTIME_ERROR",
}

_NEEDS_USER_FIX_CODES = {
    "ARK_API_KEY_MISSING",
    "ARTIFACT_CHAIN_INVALID",
    "ARTIFACT_NOT_FOUND",
    "DOCUMENT_CONTEXT_REQUIRED",
    "DOCUMENT_COVERAGE_REQUIRED",
    "DOCUMENTS_REQUIRED",
    "NO_RELEVANT_DOCUMENTS",
    "NO_USABLE_EVIDENCE",
    "RETRIEVAL_INVALID_RESPONSE",
    "RETRIEVAL_REQUEST_REJECTED",
    "STATE_ARTIFACT_MISMATCH",
}


def recovery_action(error: Mapping[str, Any] | None) -> RecoveryAction:
    if not error:
        return "needs_user_fix"
    explicit = str(error.get("recovery_action") or "")
    if explicit in {"resume_checkpoint", "new_revision", "needs_user_fix"}:
        return cast(RecoveryAction, explicit)
    code = str(error.get("code") or "")
    if code in _NEW_REVISION_CODES:
        return "new_revision"
    if code in _NEEDS_USER_FIX_CODES:
        return "needs_user_fix"
    if code in _TRANSIENT_CODES or code.endswith("_CIRCUIT_OPEN"):
        return "resume_checkpoint"
    if error.get("retryable") is True:
        return "resume_checkpoint"
    return "needs_user_fix"


def error_with_recovery(error: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(error)
    result["recovery_action"] = recovery_action(result)
    return result
