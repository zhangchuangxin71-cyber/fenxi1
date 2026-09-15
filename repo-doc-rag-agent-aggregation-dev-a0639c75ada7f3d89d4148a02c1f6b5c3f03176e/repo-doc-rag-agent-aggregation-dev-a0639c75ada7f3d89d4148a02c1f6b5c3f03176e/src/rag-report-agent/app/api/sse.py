import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


class EventEmitter:
    def __init__(self):
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._closed = False
        self._start_time = time.time()

    async def emit(self, event: str, data: dict):
        if self._closed:
            return
        await self._queue.put({"event": event, "data": json.dumps(data, ensure_ascii=False)})

    async def close(self):
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)

    async def stream(self) -> AsyncIterator[dict]:
        while True:
            item = await self._queue.get()
            if item is None:
                break
            yield item

    async def emit_stream_start(self, conversation_id: str, session_id: str):
        await self.emit(
            "stream_start",
            {"conversation_id": conversation_id, "session_id": session_id, "created_at": iso_now()},
        )

    async def emit_step(self, step: str, message: str):
        await self.emit("step", {"step": step, "message": message})

    async def emit_intent(self, intent_type: str, confidence: float | None = None, message: str | None = None):
        payload = {"intent_type": intent_type}
        if confidence is not None:
            payload["confidence"] = confidence
        if message:
            payload["message"] = message
        await self.emit("intent", payload)

    async def emit_intents(self, intents: list[dict]):
        await self.emit("intent", intents)

    async def emit_text_delta(self, delta: str):
        await self.emit("text_delta", {"delta": delta})

    async def emit_report_text_delta(self, delta: str):
        await self.emit("report_text_delta", {"delta": delta})

    async def emit_thinking_delta(self, delta: str):
        await self.emit("thinking_delta", {"delta": delta})

    async def emit_outline_delta(self, delta: str):
        await self.emit("outline_delta", {"delta": delta})

    async def emit_outline_complete(self, outline: dict):
        await self.emit("outline_complete", {"outline": outline})

    async def emit_report_start(self, message_id: str | None = None):
        await self.emit("report_start", {"message_id": message_id or ""})

    async def emit_report_end(self, message_id: str | None = None):
        await self.emit("report_end", {"message_id": message_id or "", "status": "completed"})

    async def emit_references(self, chunks: list):
        by_doc: dict[str, dict] = {}
        for chunk in chunks:
            doc_id = chunk.document_id
            if doc_id not in by_doc:
                by_doc[doc_id] = {
                    "doc_id": doc_id,
                    "doc_name": chunk.document_name,
                    "chunk_ids": [],
                }
            by_doc[doc_id]["chunk_ids"].append(chunk.chunk_id)
        await self.emit("references", {"references": list(by_doc.values())})

    async def emit_error(self, code: int, err_type: str, message: str, fatal: bool = True):
        await self.emit(
            "error",
            {"code": code, "type": err_type, "message": message, "fatal": fatal},
        )

    async def emit_stream_end(self, usage: dict | None, finish_reason: str = "complete"):
        await self.emit(
            "stream_end",
            {
                "usage": usage or {},
                "finish_reason": finish_reason,
                "duration_ms": int((time.time() - self._start_time) * 1000),
            },
        )
