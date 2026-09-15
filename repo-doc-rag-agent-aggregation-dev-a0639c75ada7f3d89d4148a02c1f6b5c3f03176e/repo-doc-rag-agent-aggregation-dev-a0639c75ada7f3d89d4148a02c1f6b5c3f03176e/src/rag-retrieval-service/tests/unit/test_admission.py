from __future__ import annotations

import asyncio

import pytest

from app.core.errors import ApiError
from app.security.admission import RetrievalAdmissionController


@pytest.mark.asyncio
async def test_admission_bounds_active_and_queued_requests() -> None:
    controller = RetrievalAdmissionController(max_active=1, max_queued=1, wait_timeout_seconds=1)
    release = asyncio.Event()

    async def hold(user_id: str) -> None:
        async with controller.admit(user_id):
            await release.wait()

    active = asyncio.create_task(hold("u1"))
    await asyncio.sleep(0)
    queued = asyncio.create_task(hold("u2"))
    await asyncio.sleep(0)

    assert controller.active_count == 1
    assert controller.queued_count == 1
    with pytest.raises(ApiError) as error:
        async with controller.admit("u3"):
            pass
    assert error.value.code == "SERVICE_OVERLOADED"

    release.set()
    await asyncio.gather(active, queued)
    assert controller.active_count == 0
    assert controller.queued_count == 0


@pytest.mark.asyncio
async def test_admission_round_robins_between_users_without_reordering_each_user() -> None:
    controller = RetrievalAdmissionController(max_active=1, max_queued=4, wait_timeout_seconds=1)
    blocker = await controller.acquire("blocker")
    order: list[str] = []

    async def run(label: str, user_id: str) -> None:
        async with controller.admit(user_id):
            order.append(label)
            await asyncio.sleep(0)

    tasks = [
        asyncio.create_task(run("u1-first", "u1")),
        asyncio.create_task(run("u1-second", "u1")),
        asyncio.create_task(run("u2-first", "u2")),
    ]
    await asyncio.sleep(0)
    await controller.release(blocker)
    await asyncio.gather(*tasks)

    assert order == ["u1-first", "u2-first", "u1-second"]


@pytest.mark.asyncio
async def test_admission_timeout_removes_ticket_without_leaking_slot() -> None:
    controller = RetrievalAdmissionController(max_active=1, max_queued=1, wait_timeout_seconds=0.01)
    blocker = await controller.acquire("u1")

    with pytest.raises(ApiError) as error:
        async with controller.admit("u2"):
            pass

    assert error.value.code == "ADMISSION_TIMEOUT"
    assert controller.queued_count == 0
    await controller.release(blocker)
    assert controller.active_count == 0
