#!/usr/bin/env python3
"""并发压测：用 source_media/1.mp4 … 14.mp4 打满 import / 切割队列。

用法（后端已启动，默认 http://127.0.0.1:8010）：

  python3 scripts/bench_concurrency.py
  python3 scripts/bench_concurrency.py --mode both --workers 14
  python3 scripts/bench_concurrency.py --base http://127.0.0.1:8010 --skip-publish

流程（每个源片一条流水线，多路并行）：
  import → 轮询 ready → manual 和/或 auto(+cut) → 可选 publish → 汇总耗时
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_BASE = "http://127.0.0.1:8010"
SOURCE_TMPL = (
    "https://example-bucket.oss-cn-region.aliyuncs.com/source_media/{n}.mp4"
)


@dataclass
class StepTiming:
    name: str
    seconds: float
    ok: bool
    detail: str = ""


@dataclass
class PipelineResult:
    index: int
    url: str
    video_id: str | None = None
    ok: bool = False
    error: str | None = None
    steps: list[StepTiming] = field(default_factory=list)
    wall_seconds: float = 0.0
    duration: float | None = None
    manual_job_id: str | None = None
    auto_job_id: str | None = None
    auto_segments: int = 0


def _req(
    base: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 120,
) -> tuple[int, dict[str, Any], float]:
    data = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    url = base.rstrip("/") + path
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
            code = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        code = exc.code
    elapsed = time.perf_counter() - t0
    try:
        payload = json.loads(raw.decode() or "{}")
    except json.JSONDecodeError:
        payload = {"raw": raw[:500].decode(errors="replace")}
    return code, payload, elapsed


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if payload.get("code") == 0 and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if "status" in payload or "video_id" in payload else {}


def poll_video(
    base: str,
    video_id: str,
    *,
    timeout: float,
    interval: float,
) -> tuple[dict[str, Any], float]:
    t0 = time.perf_counter()
    last: dict[str, Any] = {}
    while time.perf_counter() - t0 < timeout:
        code, payload, _ = _req(base, "GET", f"/api/v1/videos/{video_id}", timeout=60)
        data = _data(payload)
        last = data
        status = data.get("status")
        if status == "ready":
            return data, time.perf_counter() - t0
        if status == "failed" or code >= 400:
            raise RuntimeError(
                f"import failed video_id={video_id} http={code} "
                f"msg={payload.get('message') or data.get('message')}"
            )
        time.sleep(interval)
    raise TimeoutError(f"import timeout video_id={video_id} last={last}")


def poll_job(
    base: str,
    job_id: str,
    want: set[str],
    *,
    timeout: float,
    interval: float,
) -> tuple[dict[str, Any], float]:
    t0 = time.perf_counter()
    last: dict[str, Any] = {}
    while time.perf_counter() - t0 < timeout:
        code, payload, _ = _req(base, "GET", f"/api/v1/jobs/{job_id}", timeout=60)
        data = _data(payload)
        last = data
        status = data.get("status")
        if status in want:
            return data, time.perf_counter() - t0
        if status == "failed" or code >= 400:
            raise RuntimeError(
                f"job failed job_id={job_id} http={code} "
                f"msg={payload.get('message') or data.get('message')}"
            )
        time.sleep(interval)
    raise TimeoutError(f"job timeout job_id={job_id} want={want} last={last}")


def run_pipeline(
    base: str,
    index: int,
    url: str,
    *,
    mode: str,
    skip_publish: bool,
    import_timeout: float,
    job_timeout: float,
    poll_interval: float,
) -> PipelineResult:
    result = PipelineResult(index=index, url=url)
    wall0 = time.perf_counter()
    try:
        # --- import ---
        code, payload, submit_s = _req(
            base, "POST", "/api/v1/videos/import", {"url": url}, timeout=30
        )
        data = _data(payload)
        if code != 200 or payload.get("code") not in (0, None):
            raise RuntimeError(f"import submit http={code} body={payload}")
        video_id = data.get("video_id")
        if not video_id:
            raise RuntimeError(f"no video_id in {payload}")
        result.video_id = video_id
        result.steps.append(
            StepTiming("import_submit", submit_s, True, f"status={data.get('status')}")
        )

        video, ready_s = poll_video(
            base, video_id, timeout=import_timeout, interval=poll_interval
        )
        result.duration = (
            float(video["duration"]) if video.get("duration") is not None else None
        )
        result.steps.append(
            StepTiming(
                "import_ready",
                ready_s,
                True,
                f"duration={result.duration} filename={video.get('filename')}",
            )
        )

        dur = float(result.duration or 0)
        # 手动：尽量切两小段；片太短则切一整段
        if mode in ("manual", "both"):
            if dur >= 4.0:
                segs = [{"start": 0.0, "end": 1.5}, {"start": 2.0, "end": 3.5}]
            elif dur >= 1.0:
                segs = [{"start": 0.0, "end": round(min(1.0, dur * 0.5), 3)}]
            else:
                segs = [{"start": 0.0, "end": round(max(0.2, dur * 0.9), 3)}]
            code, payload, sub_s = _req(
                base,
                "POST",
                "/api/v1/jobs/manual",
                {"video_id": video_id, "segments": segs},
                timeout=30,
            )
            job = _data(payload)
            if code != 200 or payload.get("code") not in (0, None):
                raise RuntimeError(f"manual submit http={code} body={payload}")
            job_id = job["job_id"]
            result.manual_job_id = job_id
            result.steps.append(StepTiming("manual_submit", sub_s, True, job_id))
            done, wait_s = poll_job(
                base,
                job_id,
                {"done"},
                timeout=job_timeout,
                interval=poll_interval,
            )
            n = len(done.get("segments") or [])
            result.steps.append(
                StepTiming("manual_done", wait_s, True, f"segments={n}")
            )
            if not skip_publish:
                code, payload, pub_s = _req(
                    base,
                    "POST",
                    f"/api/v1/jobs/{job_id}/publish",
                    {"oss_key": "video-clip/bench/"},
                    timeout=180,
                )
                ok = code == 200 and payload.get("code") in (0, None)
                pub = _data(payload)
                result.steps.append(
                    StepTiming(
                        "manual_publish",
                        pub_s,
                        ok,
                        f"published={len(pub.get('published') or [])}",
                    )
                )
                if not ok:
                    raise RuntimeError(f"manual publish http={code} body={payload}")

        if mode in ("auto", "both"):
            code, payload, sub_s = _req(
                base,
                "POST",
                "/api/v1/jobs/auto",
                {"video_id": video_id, "detector": "content"},
                timeout=30,
            )
            job = _data(payload)
            if code != 200 or payload.get("code") not in (0, None):
                raise RuntimeError(f"auto submit http={code} body={payload}")
            job_id = job["job_id"]
            result.auto_job_id = job_id
            result.steps.append(StepTiming("auto_submit", sub_s, True, job_id))
            preview, prev_s = poll_job(
                base,
                job_id,
                {"preview"},
                timeout=job_timeout,
                interval=poll_interval,
            )
            n_prev = len(preview.get("segments") or [])
            result.steps.append(
                StepTiming("auto_preview", prev_s, True, f"segments={n_prev}")
            )
            code, payload, cut_s = _req(
                base, "POST", f"/api/v1/jobs/{job_id}/cut", {}, timeout=30
            )
            if code != 200 or payload.get("code") not in (0, None):
                raise RuntimeError(f"cut submit http={code} body={payload}")
            result.steps.append(StepTiming("auto_cut_submit", cut_s, True, ""))
            done, done_s = poll_job(
                base,
                job_id,
                {"done"},
                timeout=job_timeout,
                interval=max(poll_interval, 2.0),
            )
            result.auto_segments = len(done.get("segments") or [])
            result.steps.append(
                StepTiming(
                    "auto_done",
                    done_s,
                    True,
                    f"segments={result.auto_segments}",
                )
            )
            if not skip_publish:
                code, payload, pub_s = _req(
                    base,
                    "POST",
                    f"/api/v1/jobs/{job_id}/publish",
                    {"oss_key": "video-clip/bench/"},
                    timeout=300,
                )
                ok = code == 200 and payload.get("code") in (0, None)
                pub = _data(payload)
                result.steps.append(
                    StepTiming(
                        "auto_publish",
                        pub_s,
                        ok,
                        f"published={len(pub.get('published') or [])}",
                    )
                )
                if not ok:
                    raise RuntimeError(f"auto publish http={code} body={payload}")

        result.ok = True
    except Exception as exc:
        result.ok = False
        result.error = str(exc)
        result.steps.append(StepTiming("error", 0.0, False, str(exc)[:300]))
    finally:
        result.wall_seconds = time.perf_counter() - wall0
    return result


def _summarize(results: list[PipelineResult]) -> dict[str, Any]:
    ok_list = [r for r in results if r.ok]
    fail_list = [r for r in results if not r.ok]
    walls = [r.wall_seconds for r in results]
    by_step: dict[str, list[float]] = {}
    for r in ok_list:
        for s in r.steps:
            if s.ok and s.name != "error":
                by_step.setdefault(s.name, []).append(s.seconds)

    def stats(vals: list[float]) -> dict[str, float]:
        if not vals:
            return {}
        return {
            "n": len(vals),
            "min": round(min(vals), 3),
            "max": round(max(vals), 3),
            "avg": round(statistics.mean(vals), 3),
            "p50": round(statistics.median(vals), 3),
        }

    return {
        "total": len(results),
        "ok": len(ok_list),
        "failed": len(fail_list),
        "wall_seconds": stats(walls),
        "steps": {k: stats(v) for k, v in sorted(by_step.items())},
        "failures": [
            {"index": r.index, "url": r.url, "error": r.error} for r in fail_list
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="视频切片服务并发压测")
    parser.add_argument("--base", default=DEFAULT_BASE, help="API Origin")
    parser.add_argument(
        "--mode",
        choices=("manual", "auto", "both"),
        default="both",
        help="每个源片跑手动 / 自动 / 两者",
    )
    parser.add_argument(
        "--from",
        dest="from_n",
        type=int,
        default=1,
        help="起始编号（含）",
    )
    parser.add_argument(
        "--to",
        dest="to_n",
        type=int,
        default=14,
        help="结束编号（含）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="并行流水线数，默认=源片数",
    )
    parser.add_argument("--skip-publish", action="store_true", help="跳过 publish")
    parser.add_argument("--import-timeout", type=float, default=600)
    parser.add_argument("--job-timeout", type=float, default=900)
    parser.add_argument("--poll-interval", type=float, default=1.5)
    parser.add_argument(
        "--out",
        default="/tmp/clip_bench_concurrency.json",
        help="结果 JSON 路径",
    )
    args = parser.parse_args()

    if args.from_n < 1 or args.to_n < args.from_n:
        print("无效的 --from/--to", file=sys.stderr)
        return 2

    # health
    code, payload, _ = _req(args.base, "GET", "/api/v1/health", timeout=20)
    if code != 200 or not _data(payload).get("ok"):
        print(f"health 失败 http={code} body={payload}", file=sys.stderr)
        return 1
    print(f"health ok  base={args.base}")

    sources = [
        (n, SOURCE_TMPL.format(n=n)) for n in range(args.from_n, args.to_n + 1)
    ]
    workers = args.workers or len(sources)
    print(
        f"sources={len(sources)} ({args.from_n}..{args.to_n})  "
        f"parallel={workers}  mode={args.mode}  publish={not args.skip_publish}"
    )

    wall0 = time.perf_counter()
    results: list[PipelineResult] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(
                run_pipeline,
                args.base,
                n,
                url,
                mode=args.mode,
                skip_publish=args.skip_publish,
                import_timeout=args.import_timeout,
                job_timeout=args.job_timeout,
                poll_interval=args.poll_interval,
            ): n
            for n, url in sources
        }
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            flag = "OK" if r.ok else "FAIL"
            print(
                f"[{flag}] #{r.index:02d} wall={r.wall_seconds:.1f}s "
                f"video={r.video_id or '-'} "
                f"{r.error or ' · '.join(f'{s.name}={s.seconds:.1f}s' for s in r.steps if s.ok)}"
            )

    results.sort(key=lambda x: x.index)
    total_wall = time.perf_counter() - wall0
    summary = _summarize(results)
    summary["total_wall_seconds"] = round(total_wall, 3)
    summary["config"] = {
        "base": args.base,
        "mode": args.mode,
        "parallel": workers,
        "from": args.from_n,
        "to": args.to_n,
        "skip_publish": args.skip_publish,
    }

    out = {
        "summary": summary,
        "results": [
            {
                **{k: v for k, v in asdict(r).items() if k != "steps"},
                "steps": [asdict(s) for s in r.steps],
            }
            for r in results
        ],
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n详细结果已写入 {args.out}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
