from scripts.load_scope_benchmark import (
    RequestObservation,
    is_retrieval_uvicorn_command,
    nearest_rank_percentile,
    summarize_requests,
)


def test_nearest_rank_percentile_does_not_understate_small_samples() -> None:
    assert nearest_rank_percentile([10.0, 20.0, 30.0, 40.0], 0.95) == 40.0


def test_pid_matching_rejects_tmux_launcher_and_accepts_uvicorn_worker() -> None:
    launcher = [
        "tmux",
        "new-session",
        "uvicorn app.api.app:app --port 8220",
    ]
    worker = [
        "/app/.venv/bin/python3",
        ".venv/bin/uvicorn",
        "app.api.app:app",
        "--port",
        "8220",
    ]

    assert not is_retrieval_uvicorn_command(launcher, port=8220)
    assert is_retrieval_uvicorn_command(worker, port=8220)


def test_request_summary_separates_http_success_from_retrieval_correctness() -> None:
    observations = [
        RequestObservation(
            request_index=0,
            latency_ms=100.0,
            status_code=200,
            chunk_count=1,
            target_document_hit=True,
            labeled_page_hit=True,
            warning_codes=[],
            llm_request_count=3,
        ),
        RequestObservation(
            request_index=1,
            latency_ms=300.0,
            status_code=200,
            chunk_count=0,
            target_document_hit=False,
            labeled_page_hit=False,
            warning_codes=["KEYWORD_PREFILTER_EMPTY"],
            llm_request_count=2,
        ),
        RequestObservation(
            request_index=2,
            latency_ms=200.0,
            status_code=503,
            chunk_count=0,
            target_document_hit=False,
            labeled_page_hit=False,
            warning_codes=[],
            llm_request_count=0,
            error="upstream unavailable",
        ),
    ]

    summary = summarize_requests(observations)

    assert summary["request_count"] == 3
    assert summary["http_success_count"] == 2
    assert summary["nonempty_result_count"] == 1
    assert summary["target_document_hit_count"] == 1
    assert summary["labeled_page_hit_count"] == 1
    assert summary["latency_ms"] == {
        "min": 100.0,
        "mean": 200.0,
        "p50": 200.0,
        "p95": 300.0,
        "max": 300.0,
    }
    assert summary["total_llm_requests"] == 5
    assert summary["warning_counts"] == {"KEYWORD_PREFILTER_EMPTY": 1}
