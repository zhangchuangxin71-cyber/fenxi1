from collections.abc import Awaitable, Callable
from typing import TypeVar

from tenacity import retry, stop_after_attempt, wait_exponential

T = TypeVar("T")


def with_retry(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    return retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.2, max=2))(fn)
