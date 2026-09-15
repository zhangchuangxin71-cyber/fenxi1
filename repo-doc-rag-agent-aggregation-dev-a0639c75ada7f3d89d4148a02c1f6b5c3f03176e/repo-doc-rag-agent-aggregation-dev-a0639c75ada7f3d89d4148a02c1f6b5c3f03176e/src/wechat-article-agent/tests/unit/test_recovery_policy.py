from __future__ import annotations

import pytest

from app.core.recovery import error_with_recovery, recovery_action


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ({"code": "ARK_STREAM_TIMEOUT", "retryable": False}, "resume_checkpoint"),
        ({"code": "RETRIEVAL_CIRCUIT_OPEN", "retryable": True}, "resume_checkpoint"),
        ({"code": "ARK_STRUCTURED_OUTPUT_INVALID", "retryable": False}, "new_revision"),
        ({"code": "INSUFFICIENT_REQUIREMENTS", "retryable": False}, "new_revision"),
        ({"code": "NO_RELEVANT_DOCUMENTS", "retryable": False}, "needs_user_fix"),
        ({"code": "DOCUMENTS_REQUIRED", "retryable": False}, "needs_user_fix"),
        ({"code": "UNKNOWN_TRANSIENT", "retryable": True}, "resume_checkpoint"),
    ],
)
def test_recovery_action_matrix(error: dict[str, object], expected: str) -> None:
    assert recovery_action(error) == expected
    assert error_with_recovery(error)["recovery_action"] == expected
