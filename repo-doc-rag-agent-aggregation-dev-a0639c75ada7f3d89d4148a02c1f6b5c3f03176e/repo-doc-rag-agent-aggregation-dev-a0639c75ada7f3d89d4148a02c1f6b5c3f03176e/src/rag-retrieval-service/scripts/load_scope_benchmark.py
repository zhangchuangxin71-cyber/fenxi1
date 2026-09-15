#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import statistics
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import psycopg


@dataclass(slots=True)
class RequestObservation:
    request_index: int
    latency_ms: float
    status_code: int | None
    chunk_count: int
    target_document_hit: bool
    labeled_page_hit: bool
    warning_codes: list[str]
    llm_request_count: int
    error: str | None = None


@dataclass(slots=True)
class ResourceSample:
    monotonic_seconds: float
    process_cpu_ticks: int
    process_rss_bytes: int
    host_busy_ticks: int
    host_total_ticks: int
    mem_available_bytes: int
    memory_psi_some_avg10: float | None
    database_connections: int | None


def nearest_rank_percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(min(1.0, max(0.0, quantile)) * len(ordered)))
    return ordered[rank - 1]


def summarize_requests(observations: list[RequestObservation]) -> dict[str, Any]:
    latencies = [item.latency_ms for item in observations]
    warning_counts = Counter(
        code for observation in observations for code in observation.warning_codes
    )

    def rounded(value: float) -> float:
        return round(value, 2)

    return {
        "request_count": len(observations),
        "http_success_count": sum(
            item.status_code is not None and 200 <= item.status_code < 300 for item in observations
        ),
        "nonempty_result_count": sum(item.chunk_count > 0 for item in observations),
        "target_document_hit_count": sum(item.target_document_hit for item in observations),
        "labeled_page_hit_count": sum(item.labeled_page_hit for item in observations),
        "latency_ms": {
            "min": rounded(min(latencies, default=0.0)),
            "mean": rounded(statistics.fmean(latencies) if latencies else 0.0),
            "p50": rounded(nearest_rank_percentile(latencies, 0.50)),
            "p95": rounded(nearest_rank_percentile(latencies, 0.95)),
            "max": rounded(max(latencies, default=0.0)),
        },
        "total_llm_requests": sum(item.llm_request_count for item in observations),
        "warning_counts": dict(sorted(warning_counts.items())),
        "status_counts": dict(
            sorted(
                Counter(
                    str(item.status_code) if item.status_code is not None else "request_error"
                    for item in observations
                ).items()
            )
        ),
        "errors": [item.error for item in observations if item.error],
    }


def _load_manifest_case(path: Path, query_id: str | None) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            if query_id is None or item.get("query_id") == query_id:
                return item
    raise ValueError(f"query_id not found in manifest: {query_id}")


def _fetch_scope_doc_ids(dsn: str, *, user_id: str, kb_id: str) -> list[str]:
    sql = """
        SELECT DISTINCT d.doc_id::text
        FROM documents d
        JOIN document_bindings b ON b.doc_id = d.doc_id
        WHERE b.user_id = %s AND b.kb_id = %s AND d.status = 'ready'
        ORDER BY d.doc_id::text
    """
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(sql, [user_id, kb_id])
        return [str(row[0]) for row in cursor.fetchall()]


def _database_stats(dsn: str) -> dict[str, int]:
    sql = """
        SELECT numbackends, xact_commit, xact_rollback, blks_read, blks_hit,
               temp_files, temp_bytes
        FROM pg_stat_database
        WHERE datname = current_database()
    """
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone()
    if row is None:
        return {}
    keys = (
        "connections",
        "transactions_committed",
        "transactions_rolled_back",
        "blocks_read",
        "blocks_hit",
        "temp_files",
        "temp_bytes",
    )
    return {key: int(value or 0) for key, value in zip(keys, row, strict=True)}


def is_retrieval_uvicorn_command(arguments: list[str], *, port: int) -> bool:
    if not arguments:
        return False
    executable = Path(arguments[0]).name
    direct_uvicorn = executable == "uvicorn"
    python_uvicorn_script = (
        executable.startswith("python")
        and len(arguments) > 1
        and Path(arguments[1]).name == "uvicorn"
    )
    python_uvicorn_module = (
        executable.startswith("python")
        and len(arguments) > 2
        and arguments[1:3] == ["-m", "uvicorn"]
    )
    if not (direct_uvicorn or python_uvicorn_script or python_uvicorn_module):
        return False
    return "app.api.app:app" in arguments and "--port" in arguments and str(port) in arguments


def _find_service_pid(port: int) -> int:
    candidates: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            arguments = [
                value.decode(errors="replace")
                for value in (entry / "cmdline").read_bytes().split(b"\x00")
                if value
            ]
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if is_retrieval_uvicorn_command(arguments, port=port):
            candidates.append(int(entry.name))
    if not candidates:
        raise RuntimeError(f"could not find retrieval uvicorn process on port {port}")
    return min(candidates)


def _process_metrics(pid: int) -> tuple[int, int]:
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    fields = stat[stat.rfind(")") + 2 :].split()
    cpu_ticks = int(fields[11]) + int(fields[12])
    status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    rss_match = re.search(r"^VmRSS:\s+(\d+)\s+kB$", status, flags=re.MULTILINE)
    if rss_match is None:
        raise RuntimeError(f"VmRSS is unavailable for pid {pid}")
    return cpu_ticks, int(rss_match.group(1)) * 1024


def _host_cpu_metrics() -> tuple[int, int]:
    values = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = values[3] + values[4]
    total = sum(values)
    return total - idle, total


def _memory_metrics() -> tuple[int, float | None]:
    memory = Path("/proc/meminfo").read_text(encoding="utf-8")
    available_match = re.search(r"^MemAvailable:\s+(\d+)\s+kB$", memory, re.MULTILINE)
    if available_match is None:
        raise RuntimeError("MemAvailable is unavailable")
    pressure_path = Path("/proc/pressure/memory")
    pressure: float | None = None
    if pressure_path.exists():
        match = re.search(r"^some\s+avg10=([0-9.]+)", pressure_path.read_text(), re.MULTILINE)
        pressure = float(match.group(1)) if match else None
    return int(available_match.group(1)) * 1024, pressure


class ResourceSampler:
    def __init__(self, *, pid: int, dsn: str, interval_seconds: float) -> None:
        self.pid = pid
        self.dsn = dsn
        self.interval_seconds = interval_seconds
        self.samples: list[ResourceSample] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> list[ResourceSample]:
        self._stop.set()
        self._thread.join(timeout=max(5.0, self.interval_seconds * 4))
        return list(self.samples)

    def _run(self) -> None:
        with psycopg.connect(self.dsn) as connection:
            connection.autocommit = True
            while not self._stop.is_set():
                try:
                    process_cpu, rss = _process_metrics(self.pid)
                    host_busy, host_total = _host_cpu_metrics()
                    mem_available, pressure = _memory_metrics()
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database()"
                        )
                        row = cursor.fetchone()
                    db_connections = int(row[0]) if row else None
                    self.samples.append(
                        ResourceSample(
                            monotonic_seconds=time.monotonic(),
                            process_cpu_ticks=process_cpu,
                            process_rss_bytes=rss,
                            host_busy_ticks=host_busy,
                            host_total_ticks=host_total,
                            mem_available_bytes=mem_available,
                            memory_psi_some_avg10=pressure,
                            database_connections=db_connections,
                        )
                    )
                except (OSError, psycopg.Error, RuntimeError):
                    pass
                self._stop.wait(self.interval_seconds)


def _summarize_resources(samples: list[ResourceSample]) -> dict[str, Any]:
    if len(samples) < 2:
        return {"sample_count": len(samples), "insufficient_samples": True}
    first, last = samples[0], samples[-1]
    duration = max(0.001, last.monotonic_seconds - first.monotonic_seconds)
    clock_ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    process_cpu_percent = (
        (last.process_cpu_ticks - first.process_cpu_ticks) / clock_ticks / duration * 100
    )
    host_total_delta = last.host_total_ticks - first.host_total_ticks
    host_cpu_percent = (
        (last.host_busy_ticks - first.host_busy_ticks) / host_total_delta * 100
        if host_total_delta > 0
        else 0.0
    )
    pressure_values = [
        item.memory_psi_some_avg10 for item in samples if item.memory_psi_some_avg10 is not None
    ]
    db_connections = [
        item.database_connections for item in samples if item.database_connections is not None
    ]
    return {
        "sample_count": len(samples),
        "sampled_duration_seconds": round(duration, 3),
        "retrieval_process_cpu_percent_one_core": round(process_cpu_percent, 2),
        "retrieval_process_cpu_percent_host_capacity": round(
            process_cpu_percent / max(1, os.cpu_count() or 1), 3
        ),
        "host_cpu_percent": round(host_cpu_percent, 2),
        "retrieval_rss_bytes": {
            "start": first.process_rss_bytes,
            "peak": max(item.process_rss_bytes for item in samples),
            "end": last.process_rss_bytes,
            "peak_growth": max(item.process_rss_bytes for item in samples)
            - first.process_rss_bytes,
        },
        "host_mem_available_bytes": {
            "start": first.mem_available_bytes,
            "minimum": min(item.mem_available_bytes for item in samples),
            "end": last.mem_available_bytes,
            "maximum_drop": first.mem_available_bytes
            - min(item.mem_available_bytes for item in samples),
        },
        "memory_psi_some_avg10_max": round(max(pressure_values), 3) if pressure_values else None,
        "database_connections": {
            "start": db_connections[0],
            "peak": max(db_connections),
            "end": db_connections[-1],
        }
        if db_connections
        else None,
    }


def _page_numbers(chunk: dict[str, Any]) -> set[int]:
    numbers: set[int] = set()
    meta = chunk.get("chunk_meta") or {}
    for value in [meta.get("page_number"), *(meta.get("page_numbers") or [])]:
        try:
            if value is not None:
                numbers.add(int(value))
        except (TypeError, ValueError):
            pass
    for value in (chunk.get("chunk_id"), chunk.get("path")):
        match = re.search(r":page:(\d+)", str(value or ""))
        if match:
            numbers.add(int(match.group(1)))
    return numbers


def _observe_response(
    *,
    request_index: int,
    latency_ms: float,
    response: httpx.Response,
    target_doc_id: str,
    labeled_pages: set[int],
) -> RequestObservation:
    try:
        body = response.json()
    except ValueError:
        body = {}
    chunks = body.get("chunks") if isinstance(body, dict) else []
    chunks = chunks if isinstance(chunks, list) else []
    target_chunks = [
        chunk
        for chunk in chunks
        if isinstance(chunk, dict)
        and target_doc_id
        in {
            str(chunk.get("document_id") or ""),
            *(str(value) for value in (chunk.get("document_ids") or [])),
        }
    ]
    returned_pages = {page for chunk in target_chunks for page in _page_numbers(chunk)}
    usage = body.get("usage") if isinstance(body, dict) else {}
    warnings = body.get("warnings") if isinstance(body, dict) else []
    return RequestObservation(
        request_index=request_index,
        latency_ms=latency_ms,
        status_code=response.status_code,
        chunk_count=len(chunks),
        target_document_hit=bool(target_chunks),
        labeled_page_hit=bool(returned_pages & labeled_pages),
        warning_codes=[str(item.get("code")) for item in warnings or [] if isinstance(item, dict)],
        llm_request_count=int((usage or {}).get("llm_request_count") or 0),
        error=None if response.status_code < 400 else response.text[:500],
    )


async def _request_once(
    *,
    client: httpx.AsyncClient,
    endpoint: str,
    payload: dict[str, Any],
    request_index: int,
    target_doc_id: str,
    labeled_pages: set[int],
) -> RequestObservation:
    started = time.perf_counter()
    try:
        response = await client.post(endpoint, json=payload)
    except httpx.HTTPError as exc:
        return RequestObservation(
            request_index=request_index,
            latency_ms=(time.perf_counter() - started) * 1000,
            status_code=None,
            chunk_count=0,
            target_document_hit=False,
            labeled_page_hit=False,
            warning_codes=[],
            llm_request_count=0,
            error=f"{type(exc).__name__}: {exc}",
        )
    return _observe_response(
        request_index=request_index,
        latency_ms=(time.perf_counter() - started) * 1000,
        response=response,
        target_doc_id=target_doc_id,
        labeled_pages=labeled_pages,
    )


async def _run_phase(
    *,
    name: str,
    endpoint: str,
    payload: dict[str, Any],
    concurrency: int,
    timeout_seconds: float,
    pid: int,
    dsn: str,
    sample_interval_seconds: float,
    target_doc_id: str,
    labeled_pages: set[int],
) -> dict[str, Any]:
    before = _database_stats(dsn)
    sampler = ResourceSampler(pid=pid, dsn=dsn, interval_seconds=sample_interval_seconds)
    sampler.start()
    limits = httpx.Limits(
        max_connections=max(1, concurrency), max_keepalive_connections=max(1, concurrency)
    )
    timeout = httpx.Timeout(timeout_seconds, connect=10.0)
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        observations = await asyncio.gather(
            *[
                _request_once(
                    client=client,
                    endpoint=endpoint,
                    payload=payload,
                    request_index=index,
                    target_doc_id=target_doc_id,
                    labeled_pages=labeled_pages,
                )
                for index in range(concurrency)
            ]
        )
    wall_seconds = time.perf_counter() - started
    samples = sampler.stop()
    after = _database_stats(dsn)
    database_delta = {
        key: after.get(key, 0) - before.get(key, 0) for key in after if key != "connections"
    }
    return {
        "name": name,
        "concurrency": concurrency,
        "wall_seconds": round(wall_seconds, 3),
        "throughput_requests_per_second": round(concurrency / wall_seconds, 4),
        "requests": summarize_requests(observations),
        "resources": _summarize_resources(samples),
        "database_delta": database_delta,
        "observations": [asdict(item) for item in observations],
    }


def _payload(
    *,
    user_id: str,
    kb_id: str,
    query: str,
    doc_ids: list[str],
    top_k: int,
    max_return_tokens: int,
    include_debug: bool,
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "kb_id": kb_id,
        "query": query,
        "doc_ids": doc_ids,
        "temp_doc_ids": [],
        "top_k": top_k,
        "max_return_tokens": max_return_tokens,
        "search_mode": "semantic",
        "options": {
            "include_document_meta": True,
            "include_debug": include_debug,
            "ensure_document_coverage": False,
        },
    }


async def _main(args: argparse.Namespace) -> dict[str, Any]:
    case = _load_manifest_case(Path(args.manifest), args.query_id)
    target_doc_id = str(case["doc_id"])
    labeled_pages = set(range(int(case["start_index"]), int(case["end_index"]) + 1))
    doc_ids = _fetch_scope_doc_ids(args.postgres_dsn, user_id=args.user_id, kb_id=args.kb_id)
    if target_doc_id not in doc_ids:
        raise RuntimeError(
            "manifest target document is not present in the configured request scope"
        )
    if len(doc_ids) > args.max_docs:
        raise RuntimeError(
            f"database scope has {len(doc_ids)} documents, exceeding --max-docs={args.max_docs}"
        )
    process_id = args.service_pid or _find_service_pid(args.service_port)
    endpoint = f"{args.base_url.rstrip('/')}/rag/v1/retrieve"
    noise = [doc_id for doc_id in doc_ids if doc_id != target_doc_id]
    small_doc_ids = [target_doc_id, *noise[: max(0, args.small_scope_size - 1)]]
    common = {
        "user_id": args.user_id,
        "kb_id": args.kb_id,
        "query": str(case["query_text"]),
        "top_k": args.top_k,
        "max_return_tokens": args.max_return_tokens,
    }
    small = await _run_phase(
        name="small_scope_correctness",
        endpoint=endpoint,
        payload=_payload(doc_ids=small_doc_ids, include_debug=True, **common),
        concurrency=1,
        timeout_seconds=args.timeout_seconds,
        pid=process_id,
        dsn=args.postgres_dsn,
        sample_interval_seconds=args.sample_interval_seconds,
        target_doc_id=target_doc_id,
        labeled_pages=labeled_pages,
    )
    if small["requests"]["labeled_page_hit_count"] != 1:
        raise RuntimeError("small-scope correctness gate failed; large-scope load test was not run")
    full_payload = _payload(doc_ids=doc_ids, include_debug=False, **common)
    single = await _run_phase(
        name="full_scope_single_request",
        endpoint=endpoint,
        payload=full_payload,
        concurrency=1,
        timeout_seconds=args.timeout_seconds,
        pid=process_id,
        dsn=args.postgres_dsn,
        sample_interval_seconds=args.sample_interval_seconds,
        target_doc_id=target_doc_id,
        labeled_pages=labeled_pages,
    )
    concurrent = await _run_phase(
        name="full_scope_30_concurrent",
        endpoint=endpoint,
        payload=full_payload,
        concurrency=args.concurrency,
        timeout_seconds=args.timeout_seconds,
        pid=process_id,
        dsn=args.postgres_dsn,
        sample_interval_seconds=args.sample_interval_seconds,
        target_doc_id=target_doc_id,
        labeled_pages=labeled_pages,
    )
    return {
        "schema_version": "1.0",
        "generated_at_epoch_seconds": int(time.time()),
        "service": {"base_url": args.base_url, "pid": process_id},
        "database": {
            "scope_document_count": len(doc_ids),
            "user_id": args.user_id,
            "kb_id": args.kb_id,
        },
        "case": {
            "query_id": case.get("query_id"),
            "query_text": case.get("query_text"),
            "target_doc_id": target_doc_id,
            "labeled_pages": sorted(labeled_pages),
        },
        "configuration": {
            "top_k": args.top_k,
            "max_return_tokens": args.max_return_tokens,
            "timeout_seconds": args.timeout_seconds,
            "concurrency": args.concurrency,
            "sample_interval_seconds": args.sample_interval_seconds,
        },
        "phases": [small, single, concurrent],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark one full document scope against a running retrieval service."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8220")
    parser.add_argument("--service-port", type=int, default=8220)
    parser.add_argument("--service-pid", type=int, default=None)
    parser.add_argument("--postgres-dsn", required=True)
    parser.add_argument("--user-id", default="eval-user")
    parser.add_argument("--kb-id", default="eval-rag-kb")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--query-id", default="q_000001")
    parser.add_argument("--small-scope-size", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=30)
    parser.add_argument("--max-docs", type=int, default=1000)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--max-return-tokens", type=int, default=8192)
    parser.add_argument("--timeout-seconds", type=float, default=1000.0)
    parser.add_argument("--sample-interval-seconds", type=float, default=0.25)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    report = asyncio.run(_main(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
