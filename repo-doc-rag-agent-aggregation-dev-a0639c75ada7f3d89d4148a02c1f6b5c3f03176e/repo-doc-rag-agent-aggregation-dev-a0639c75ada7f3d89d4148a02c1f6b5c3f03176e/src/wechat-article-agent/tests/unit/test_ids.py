from uuid import UUID

from app.core.ids import advisory_lock_key, thread_id_for_session


def test_thread_id_is_stable_and_session_specific() -> None:
    namespace = UUID("ee4b563b-9a8f-4d3c-9e04-7bd912566bf5")
    first = thread_id_for_session(namespace, "session-a")
    assert first == thread_id_for_session(namespace, "session-a")
    assert first != thread_id_for_session(namespace, "session-b")
    assert advisory_lock_key(first) == advisory_lock_key(first)
