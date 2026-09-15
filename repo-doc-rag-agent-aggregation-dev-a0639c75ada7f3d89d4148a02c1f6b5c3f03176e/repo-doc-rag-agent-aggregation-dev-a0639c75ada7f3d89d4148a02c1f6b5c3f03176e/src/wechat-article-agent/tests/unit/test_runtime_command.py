from __future__ import annotations

import pytest

from app.adapter.service import _cancel_confirmation_status
from app.runtime.client import _command


def test_failed_retry_uses_state_update_without_interrupt_resume() -> None:
    command = _command(state_update={"response_id": "resp_new", "status": "running"})

    assert command is not None
    assert command["update"] == {"response_id": "resp_new", "status": "running"}
    assert "resume" not in command


def test_resume_and_state_update_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        _command(resume={"decision": "approve"}, state_update={"response_id": "resp_new"})


def test_cancel_race_is_success_when_monitor_already_finished_cancellation() -> None:
    assert _cancel_confirmation_status("cancelled") == 200
    assert _cancel_confirmation_status("cancelling") == 202
