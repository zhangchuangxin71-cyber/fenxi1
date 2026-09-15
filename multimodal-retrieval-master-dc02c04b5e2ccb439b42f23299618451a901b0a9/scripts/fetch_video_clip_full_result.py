#!/usr/bin/env python3
"""
提交视频 CLIP 异步任务，轮询完成后拉取结果（仅整片 embedding）并保存为 JSON。

用法（项目根目录，embedding_service 已启动）：

  python scripts/fetch_video_clip_full_result.py \\
    --object-key "oss测试文件/工地上作业的工人2.mp4" \\
    --media-id gongdi_worker_2

  python scripts/fetch_video_clip_full_result.py --job-id <已有 job_id> -o runtime/outputs/out.json

  python scripts/fetch_video_clip_full_result.py --base-url http://127.0.0.1:8035 ...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = PROJECT_ROOT / "runtime" / "outputs"


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


def base_url_from_args(explicit: str | None) -> str:
    if explicit:
        return explicit.rstrip("/")
    port = os.environ.get("EMBEDDING_SERVICE_PORT", "8030")
    env_base = os.environ.get("EMBEDDING_BASE_URL")
    if env_base:
        return env_base.rstrip("/")
    return f"http://127.0.0.1:{port}"


def wait_health(base: str, timeout_s: float = 300.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = requests.get(f"{base}/health", timeout=5)
            if r.status_code == 200:
                body = r.json()
                if body.get("status") == "ok":
                    print(f"[ok] health: {body.get('status')}")
                    return
        except requests.RequestException:
            pass
        time.sleep(2)
    raise TimeoutError(f"service not ready: {base}")


def submit_job(base: str, payload: dict[str, Any]) -> str:
    r = requests.post(f"{base}/v1/videos/clip/embed-jobs", json=payload, timeout=60)
    r.raise_for_status()
    job_id = r.json()["job_id"]
    print(f"[submit] job_id={job_id}")
    return job_id


def poll_job(
    base: str,
    job_id: str,
    *,
    poll_interval: float = 3.0,
    timeout_s: float = 3600.0,
) -> None:
    url = f"{base}/v1/videos/clip/embed-jobs/{job_id}"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        body = r.json()
        status = body.get("status")
        print(f"[poll] {job_id} status={status}")
        if status == "succeeded":
            size = body.get("result_size_bytes")
            if size is not None:
                print(f"[poll] result_size_bytes={size} ({size / 1024:.1f} KB)")
            return
        if status == "failed":
            raise RuntimeError(f"job failed: {json.dumps(body, ensure_ascii=False)}")
        time.sleep(poll_interval)
    raise TimeoutError(f"job timeout: {job_id}")


def fetch_result(base: str, job_id: str) -> dict[str, Any]:
    url = f"{base}/v1/videos/clip/embed-jobs/{job_id}/result"
    print(f"[fetch] GET {url}")
    t0 = time.perf_counter()
    r = requests.get(url, timeout=600)
    r.raise_for_status()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    raw = r.content
    payload = r.json()
    print(f"[fetch] {len(raw)} bytes, {elapsed_ms:.0f} ms")
    return payload


def extract_embedding_dim(payload: dict[str, Any]) -> int:
    embedding = payload.get("embedding")
    if isinstance(embedding, list):
        return len(embedding)
    video_level = payload.get("video_level")
    if isinstance(video_level, dict) and isinstance(video_level.get("embedding"), list):
        return len(video_level["embedding"])
    return int(payload.get("vector_dim") or 0)


def summarize_result(payload: dict[str, Any]) -> dict[str, Any]:
    dim = extract_embedding_dim(payload)
    return {
        "vector_dim": dim,
        "has_embedding": dim > 0,
        "embedding_model": payload.get("embedding_model"),
        "media_id": payload.get("media_id"),
    }


def save_result(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def default_output_path(media_id: str, job_id: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in media_id)[:64]
    return DEFAULT_OUT_DIR / f"video_clip_{safe_id}_{job_id}_{stamp}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=None, help="默认 http://127.0.0.1:$EMBEDDING_SERVICE_PORT")
    parser.add_argument("--job-id", default=None, help="已有任务 ID，跳过提交与轮询")
    parser.add_argument("--object-key", default=None, help="OSS 对象键（提交任务时必填）")
    parser.add_argument("--media-id", default=None, help="业务 media_id，默认 bench-<timestamp>")
    parser.add_argument("--content-type", default="video/mp4")
    parser.add_argument(
        "--return-vector",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="提交时 return_vector；false 时结果 embedding 为 null",
    )
    parser.add_argument("--write-vector", action="store_true", help="提交时 write_vector 写入 Milvus")
    parser.add_argument("--sample-fps", type=float, default=1.0)
    parser.add_argument("--max-frames", type=int, default=16)
    parser.add_argument("--top-k-frames", type=int, default=4)
    parser.add_argument("--segment-seconds", type=int, default=5)
    parser.add_argument("-o", "--output", default=None, help="输出 JSON 路径")
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--skip-health", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    base = base_url_from_args(args.base_url)

    if not args.job_id and not args.object_key:
        parser.error("需要 --object-key（提交新任务）或 --job-id（拉取已有任务）")

    media_id = args.media_id or f"bench-{int(time.time())}"

    try:
        if not args.skip_health:
            wait_health(base)

        job_id = args.job_id
        if not job_id:
            payload = {
                "media_id": media_id,
                "object_key": args.object_key,
                "content_type": args.content_type,
                "return_vector": args.return_vector,
                "write_vector": args.write_vector,
                "configuration": {
                    "sample_fps": args.sample_fps,
                    "max_frames": args.max_frames,
                    "top_k_frames": args.top_k_frames,
                    "segment_seconds": args.segment_seconds,
                },
            }
            print(f"[submit] media_id={media_id} object_key={args.object_key!r}")
            job_id = submit_job(base, payload)
            poll_job(base, job_id, poll_interval=args.poll_interval, timeout_s=args.timeout)
        else:
            media_id = media_id or job_id

        result = fetch_result(base, job_id)
        stats = summarize_result(result)
        print(
            "[summary] "
            f"media_id={stats['media_id']} "
            f"model={stats['embedding_model']} "
            f"vector_dim={stats['vector_dim']} "
            f"has_embedding={stats['has_embedding']}"
        )

        if not stats["has_embedding"]:
            print(
                "[warn] 无 embedding；若为新提交请使用 --return-vector，"
                "或检查任务是否 return_vector=false"
            )

        out_path = Path(args.output) if args.output else default_output_path(media_id, job_id)
        save_result(out_path, result)
        size_kb = out_path.stat().st_size / 1024
        print(f"[saved] {out_path.resolve()} ({size_kb:.1f} KB)")
        return 0
    except (requests.RequestException, TimeoutError, RuntimeError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
