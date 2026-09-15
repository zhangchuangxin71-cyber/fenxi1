import uuid


def new_conversation_id() -> str:
    return f"conv_{uuid.uuid4().hex}"
