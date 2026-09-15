from __future__ import annotations

from uuid import UUID, uuid4, uuid5


def prefixed_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def thread_id_for_session(namespace: UUID, session_id: str) -> str:
    return str(uuid5(namespace, session_id))


def advisory_lock_key(thread_id: str) -> int:
    value = UUID(thread_id).int & ((1 << 63) - 1)
    return value if value < (1 << 63) else value - (1 << 64)
