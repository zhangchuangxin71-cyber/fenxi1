from __future__ import annotations

from app.security.rate_limit import TokenBucketLimiter


def test_rate_limit_is_keyed_only_by_user_id() -> None:
    now = [0.0]
    limiter = TokenBucketLimiter(
        enabled=True,
        per_minute=60,
        burst=1,
        clock=lambda: now[0],
    )

    assert limiter.allow("user-1") == (True, 0.0)
    allowed, retry_after = limiter.allow("user-1")
    assert allowed is False
    assert retry_after == 1.0
    assert limiter.allow("user-2") == (True, 0.0)


def test_rate_limit_cleans_idle_buckets() -> None:
    now = [0.0]
    limiter = TokenBucketLimiter(
        enabled=True,
        per_minute=60,
        burst=1,
        idle_ttl_seconds=10,
        clock=lambda: now[0],
    )
    limiter.allow("old-user")
    now[0] = 11.0

    limiter.allow("new-user")

    assert limiter.bucket_count == 1
