from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, cast

from langgraph_sdk import get_client
from langgraph_sdk.schema import Command


class GraphRuntimeClient:
    """The only application boundary that knows the Agent Server SDK surface."""

    def __init__(self, *, url: str, assistant_id: str, thread_ttl_minutes: int = 4320) -> None:
        self._client = get_client(url=url)
        self.assistant_id = assistant_id
        self.thread_ttl_minutes = thread_ttl_minutes

    async def ensure_thread(self, thread_id: str) -> Mapping[str, Any]:
        return await self._client.threads.create(
            thread_id=thread_id,
            if_exists="do_nothing",
            ttl={"strategy": "delete", "ttl": self.thread_ttl_minutes},
            graph_id=self.assistant_id,
        )

    async def refresh_thread_ttl(self, thread_id: str) -> None:
        await self._client.threads.update(
            thread_id,
            metadata={},
            ttl={"strategy": "delete", "ttl": self.thread_ttl_minutes},
            return_minimal=True,
        )

    async def create_run(
        self,
        thread_id: str,
        *,
        input: Mapping[str, Any] | None = None,
        resume: Any = None,
        state_update: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        after_seconds: int | None = 1,
    ) -> Mapping[str, Any]:
        command = _command(resume=resume, state_update=state_update)
        result = await self._client.runs.create(
            thread_id,
            self.assistant_id,
            input=dict(input) if input is not None else None,
            command=command,
            context=dict(context) if context else None,
            metadata=dict(metadata) if metadata else None,
            stream_mode=["messages", "custom", "updates"],
            stream_resumable=True,
            multitask_strategy="reject",
            after_seconds=after_seconds,
        )
        return cast(Mapping[str, Any], result)

    async def start_stream(
        self,
        thread_id: str,
        *,
        input: Mapping[str, Any] | None = None,
        resume: Any = None,
        state_update: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, AsyncIterator[Any]]:
        command = _command(resume=resume, state_update=state_update)
        created: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def on_run_created(value: Any) -> None:
            if not created.done():
                created.set_result(str(value["run_id"]))

        stream = self._client.runs.stream(
            thread_id,
            self.assistant_id,
            input=dict(input) if input is not None else None,
            command=command,
            context=dict(context) if context else None,
            metadata=dict(metadata) if metadata else None,
            stream_mode=["messages", "custom", "updates"],
            stream_resumable=True,
            multitask_strategy="reject",
            after_seconds=1,
            on_disconnect="continue",
            on_run_created=on_run_created,
            version="v1",
        )
        iterator = stream.__aiter__()

        async def events() -> AsyncIterator[Any]:
            async for part in iterator:
                yield part

        first_task: asyncio.Future[Any] = asyncio.ensure_future(anext(iterator))
        done, _ = await asyncio.wait({first_task, created}, return_when=asyncio.FIRST_COMPLETED)
        if created in done:
            run_id = created.result()
        else:
            first = first_task.result()
            data = getattr(first, "data", None)
            if getattr(first, "event", "") != "metadata" or not isinstance(data, Mapping):
                first_task.cancel()
                raise RuntimeError("Agent Server did not identify the created streaming run")
            run_id = str(data["run_id"])

        async def with_first() -> AsyncIterator[Any]:
            if not first_task.done():
                first = await first_task
                yield first
            else:
                yield first_task.result()
            async for part in events():
                yield part

        return run_id, with_first()

    async def stream_run(
        self,
        thread_id: str,
        run_id: str,
        *,
        last_event_id: str | None = None,
        stream_mode: Sequence[str] = ("messages", "custom", "updates"),
    ) -> AsyncIterator[Any]:
        async for part in self._client.runs.join_stream(
            thread_id,
            run_id,
            cancel_on_disconnect=False,
            stream_mode=list(stream_mode),  # type: ignore[arg-type]
            last_event_id=last_event_id,
        ):
            yield part

    async def run(self, thread_id: str, run_id: str) -> Mapping[str, Any]:
        return await self._client.runs.get(thread_id, run_id)

    async def join(self, thread_id: str, run_id: str) -> Mapping[str, Any]:
        return await self._client.runs.join(thread_id, run_id)

    async def thread(self, thread_id: str) -> Mapping[str, Any]:
        return await self._client.threads.get(thread_id)

    async def state(self, thread_id: str) -> Mapping[str, Any]:
        return await self._client.threads.get_state(thread_id)

    async def cancel_run(self, thread_id: str, run_id: str, *, wait: bool = True) -> None:
        await self._client.runs.cancel(thread_id, run_id, wait=wait, action="interrupt")

    async def delete_thread(self, thread_id: str) -> None:
        await self._client.threads.delete(thread_id)

    async def close(self) -> None:
        await self._client.aclose()


def _command(*, resume: Any = None, state_update: Mapping[str, Any] | None = None) -> Command | None:
    if resume is not None and state_update is not None:
        raise ValueError("resume and state_update are mutually exclusive")
    if resume is not None:
        return Command(resume=resume)
    if state_update is not None:
        return Command(update=dict(state_update))
    return None
