"""Process-wide concurrency helpers."""

from videoaudiotext.concurrency.limits import global_job_workers, uvicorn_workers
from videoaudiotext.concurrency.pools import (
    GLOBAL_JOB_WORKERS,
    parallel_map,
    parallel_starmap,
    submit_api_job,
)

__all__ = [
    "GLOBAL_JOB_WORKERS",
    "global_job_workers",
    "parallel_map",
    "parallel_starmap",
    "submit_api_job",
    "uvicorn_workers",
]
