from __future__ import annotations

import asyncio

import pytest

from app.core.errors import AppError
from app.runtime.admission import RunAdmissionController


@pytest.mark.asyncio
async def test_admission_queues_until_active_permit_is_released() -> None:
    admission = RunAdmissionController(max_active=1, max_queued=1, wait_timeout=1)
    first = await admission.acquire()
    waiting = asyncio.create_task(admission.acquire())
    await asyncio.sleep(0)

    assert admission.active == 1
    assert admission.queued == 1
    await first.release()
    second = await waiting
    assert admission.active == 1
    await second.release()
    assert admission.active == 0


@pytest.mark.asyncio
async def test_admission_rejects_when_queue_is_full() -> None:
    admission = RunAdmissionController(max_active=1, max_queued=1, wait_timeout=1)
    first = await admission.acquire()
    waiting = asyncio.create_task(admission.acquire())
    await asyncio.sleep(0)

    with pytest.raises(AppError) as raised:
        await admission.acquire()
    assert raised.value.code == "RUN_QUEUE_FULL"

    await first.release()
    second = await waiting
    await second.release()
