#!/usr/bin/env python3
"""
视频 CLIP 任务基准：提交（可选）、轮询、GET /result 耗时与 embedding 结构检查。

结果默认保存到 runtime/logs/：
  - benchmark_video_clip_<时间戳>.log
  - benchmark_video_clip_<时间戳>.json
  - 加 --save-vector 时另存完整 API JSON

用法（项目根目录，服务已启动）：
  python scripts/benchmark_video_clip_jobs.py \\
    --object-key "oss测试文件/工地上作业的工人2.mp4"

  python scripts/benchmark_video_clip_jobs.py --job-id <job_id>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "runtime" / "logs"


class RunLogger:
    def __init__(self, log_path: Path | None) -> None:
        self.log_path = log_path
        self._file: TextIO | None = None
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = log_path.open("w", encoding="utf-8")

    def log(self, msg: str = "") -> None:
        print(msg)
        if self._file:
            self._file.write(msg + "\n")
            self._file.flush()

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None


_run: RunLogger = RunLogger(None)


def load_dotenv() -> None:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _embedding_dim(payload: dict[str, Any]) -> int:
    embedding = payload.get("embedding")
    if isinstance(embedding, list):
        return len(embedding)
    video_level = payload.get("video_level")
    if isinstance(video_level, dict) and isinstance(video_level.get("embedding"), list):
        return len(video_level["embedding"])
    return int(payload.get("vector_dim") or 0)


@dataclass
class ResultReport:
    vector_dim: int = 0
    has_embedding: bool = False
    has_legacy_levels: bool = False
    checks_ok: bool = True
    check_notes: list[str] = field(default_factory=list)

    def print_summary(self) -> None:
        _run.log(
            f"    embedding: dim={self.vector_dim}  present={self.has_embedding}  "
            f"legacy_frame_or_segment={self.has_legacy_levels}"
        )
        status = "OK" if self.checks_ok else "FAIL"
        _run.log(f"    预期: 仅顶层 embedding（无 frame_level/segment_level）  →  [{status}]")
        for note in self.check_notes:
            _run.log(f"      - {note}")


def analyze_result(payload: dict[str, Any]) -> ResultReport:
    rep = ResultReport()
    if not payload:
        rep.checks_ok = False
        rep.check_notes.append("响应为空")
        return rep

    rep.vector_dim = _embedding_dim(payload)
    rep.has_embedding = rep.vector_dim > 0
    rep.has_legacy_levels = bool(payload.get("frame_level") or payload.get("segment_level"))

    if not rep.has_embedding:
        rep.checks_ok = False
        rep.check_notes.append("缺少 embedding（检查 POST return_vector=true）")
    if rep.has_legacy_levels:
        rep.checks_ok = False
        rep.check_notes.append("不应再返回 frame_level / segment_level")
    if payload.get("video_level") and not payload.get("embedding"):
        rep.check_notes.append("检测到旧字段 video_level，请升级 embedding_service")

    return rep


@dataclass
class FetchBenchmark:
    http_ms: float
    body_bytes: int
    json_parse_ms: float
    status: int
    report: ResultReport
    error: str | None = None
    saved_vector_path: str | None = None


def fetch_result(
    session: requests.Session,
    base: str,
    job_id: str,
) -> tuple[FetchBenchmark, dict[str, Any] | None]:
    url = f"{base}/v1/videos/clip/embed-jobs/{job_id}/result"
    t0 = time.perf_counter()
    try:
        resp = session.get(url, timeout=600)
        http_ms = (time.perf_counter() - t0) * 1000
        raw = resp.content
        t1 = time.perf_counter()
        payload: dict[str, Any] | None = None
        parse_ms = 0.0
        if raw:
            payload = json.loads(raw)
            parse_ms = (time.perf_counter() - t1) * 1000
        report = analyze_result(payload or {})
        err = None if resp.ok else raw[:300].decode("utf-8", errors="replace")
        return (
            FetchBenchmark(
                http_ms=http_ms,
                body_bytes=len(raw),
                json_parse_ms=parse_ms,
                status=resp.status_code,
                report=report,
                error=err,
            ),
            payload,
        )
    except (requests.RequestException, json.JSONDecodeError) as exc:
        http_ms = (time.perf_counter() - t0) * 1000
        return (
            FetchBenchmark(
                http_ms=http_ms,
                body_bytes=0,
                json_parse_ms=0,
                status=0,
                report=ResultReport(checks_ok=False, check_notes=[str(exc)]),
                error=str(exc),
            ),
            None,
        )


def poll_until_done(
    session: requests.Session, base: str, job_id: str, poll_interval: float, max_wait: float
) -> str:
    url = f"{base}/v1/videos/clip/embed-jobs/{job_id}"
    deadline = time.perf_counter() + max_wait
    while time.perf_counter() < deadline:
        r = session.get(url, timeout=30)
        status = r.json().get("status")
        _run.log(f"  poll status={status}")
        if status in ("succeeded", "failed"):
            return status
        time.sleep(poll_interval)
    return "timeout"


def submit_job(
    session: requests.Session,
    base: str,
    *,
    media_id: str,
    object_key: str,
    content_type: str,
    return_vector: bool,
    configuration: dict[str, Any],
) -> str | None:
    body = {
        "media_id": media_id,
        "object_key": object_key,
        "content_type": content_type,
        "return_vector": return_vector,
        "write_vector": False,
        "configuration": configuration,
    }
    t0 = time.perf_counter()
    r = session.post(f"{base}/v1/videos/clip/embed-jobs", json=body, timeout=60)
    ms = (time.perf_counter() - t0) * 1000
    _run.log(f"  POST submit: {r.status_code}  {ms:.1f}ms  return_vector={return_vector}")
    if not r.ok:
        _run.log(f"  {r.text[:500]}")
        return None
    job_id = r.json().get("job_id")
    _run.log(f"  job_id={job_id}")
    return job_id


def default_output_paths() -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = LOG_DIR / f"benchmark_video_clip_{stamp}"
    return stem.with_suffix(".log"), stem.with_suffix(".json")


def main() -> int:
    global _run
    load_dotenv()
    parser = argparse.ArgumentParser(description="视频 CLIP /result 耗时与 embedding 结构检查")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--object-key", default=None)
    parser.add_argument("--content-type", default="video/mp4")
    parser.add_argument("--media-id", default=None)
    parser.add_argument("--job-id", default=None)
    parser.add_argument("--return-vector", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-frames", type=int, default=16)
    parser.add_argument("--top-k-frames", type=int, default=4)
    parser.add_argument("--segment-seconds", type=int, default=5)
    parser.add_argument("--sample-fps", type=float, default=1.0)
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument("--max-wait", type=float, default=3600.0)
    parser.add_argument("-o", "--output", default=None, help="文本报告路径")
    parser.add_argument("--json-output", default=None, help="JSON 报告路径")
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--save-vector", action="store_true", help="另存完整 API JSON")
    parser.add_argument("--vector-output", default=None, help="向量 JSON 输出路径")
    args = parser.parse_args()

    log_path = json_path = None
    if not args.no_save:
        if args.output:
            log_path = Path(args.output)
            json_path = Path(args.json_output) if args.json_output else log_path.with_suffix(".json")
        else:
            log_path, json_path = default_output_paths()

    _run = RunLogger(log_path)
    if log_path:
        _run.log(f"# 报告开始 {datetime.now().isoformat(timespec='seconds')}")

    port = os.environ.get("EMBEDDING_SERVICE_PORT", "8030")
    base = (args.base_url or f"http://127.0.0.1:{port}").rstrip("/")
    session = requests.Session()
    media_id = args.media_id

    _run.log(f"Base URL: {base}")
    try:
        h = session.get(f"{base}/health", timeout=10)
        _run.log(f"Health: {h.status_code} {h.json() if h.ok else h.text[:100]}")
    except requests.RequestException as exc:
        _run.log(f"Health FAILED: {exc}")
        _run.close()
        return 1

    job_id = args.job_id
    try:
        if not job_id:
            if not args.object_key:
                _run.log("需要 --object-key 或 --job-id")
                return 1
            media_id = media_id or f"bench-{int(time.time())}"
            _run.log(f"\n提交任务 media_id={media_id} object_key={args.object_key!r} ...")
            job_id = submit_job(
                session,
                base,
                media_id=media_id,
                object_key=args.object_key,
                content_type=args.content_type,
                return_vector=args.return_vector,
                configuration={
                    "sample_fps": args.sample_fps,
                    "max_frames": args.max_frames,
                    "top_k_frames": args.top_k_frames,
                    "segment_seconds": args.segment_seconds,
                },
            )
            if not job_id:
                return 1
            _run.log("\n等待任务完成...")
            final = poll_until_done(session, base, job_id, args.poll_interval, args.max_wait)
            if final != "succeeded":
                _run.log(f"任务未成功: {final}")
                return 1

        bench, payload = fetch_result(session, base, job_id)
        _run.log("\n  === GET /result ===\n")
        size = f"{bench.body_bytes / 1024:.1f} KB" if bench.body_bytes else "0"
        ok = "OK" if bench.report.checks_ok and not bench.error else "FAIL"
        _run.log(
            f"  HTTP {bench.status}  {bench.http_ms:.1f} ms  parse {bench.json_parse_ms:.1f} ms  "
            f"size {size}  [{ok}]"
            + (f"  ERR: {bench.error[:80]}" if bench.error else "")
        )
        bench.report.print_summary()

        if args.save_vector and payload:
            vector_path = Path(args.vector_output) if args.vector_output else (
                LOG_DIR / f"video_clip_result_{job_id}.json"
            )
            vector_path.parent.mkdir(parents=True, exist_ok=True)
            vector_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            bench.saved_vector_path = str(vector_path.resolve())
            _run.log(f"\n  已保存向量 JSON: {bench.saved_vector_path}")

        all_ok = bench.report.checks_ok and not bench.error
        _run.log("\n" + "=" * 72)
        _run.log("检查通过。" if all_ok else "检查未通过，见上方 FAIL 说明。")
        _run.log("=" * 72)

        if json_path:
            report = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "base_url": base,
                "job_id": job_id,
                "media_id": media_id,
                "return_vector": args.return_vector if not args.job_id else None,
                "all_ok": all_ok,
                "fetch": {
                    "http_ms": round(bench.http_ms, 2),
                    "json_parse_ms": round(bench.json_parse_ms, 2),
                    "body_bytes": bench.body_bytes,
                    "status": bench.status,
                    "report": asdict(bench.report),
                    "vector_file": bench.saved_vector_path,
                    "error": bench.error,
                },
            }
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            _run.log(f"\n已保存 JSON 报告: {json_path.resolve()}")

        return 0 if all_ok else 1
    finally:
        _run.close()


if __name__ == "__main__":
    sys.exit(main())
