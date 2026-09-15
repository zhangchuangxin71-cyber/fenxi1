#!/usr/bin/env python3
"""OSS 媒体写入 Milvus（embedding API）并用 CLIP 文本向量做检索验证。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


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


def wait_health(base_url: str, timeout_s: float = 300.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url.rstrip('/')}/health", timeout=5)
            if r.status_code == 200:
                body = r.json()
                if body.get("status") == "ok" or body.get("vector_store"):
                    print(f"[ok] health: {body}")
                    return
        except requests.RequestException:
            pass
        time.sleep(3)
    raise TimeoutError(f"service not ready: {base_url}")


def submit_and_wait(
    base_url: str,
    path: str,
    payload: dict,
    *,
    poll_interval: float = 2.0,
    timeout_s: float = 1800.0,
) -> dict:
    r = requests.post(f"{base_url.rstrip('/')}{path}", json=payload, timeout=60)
    r.raise_for_status()
    job_id = r.json()["job_id"]
    print(f"[job] submitted {path} job_id={job_id}")

    status_url = f"{base_url.rstrip('/')}{path}/{job_id}"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        s = requests.get(status_url, timeout=30)
        s.raise_for_status()
        body = s.json()
        status = body.get("status")
        print(f"[job] {job_id} status={status}")
        if status == "succeeded":
            milvus = body.get("milvus") or (body.get("result") or {}).get("milvus")
            if milvus:
                print(f"[milvus] {json.dumps(milvus, ensure_ascii=False)}")
            return body
        if status == "failed":
            raise RuntimeError(f"job failed: {json.dumps(body, ensure_ascii=False)}")
        time.sleep(poll_interval)
    raise TimeoutError(f"job timeout: {job_id}")


def encode_query_clip(base_url: str, text: str) -> np.ndarray:
    r = requests.post(
        f"{base_url.rstrip('/')}/v1/query/clip/embed",
        json={"text": text, "return_vector": True},
        timeout=120,
    )
    r.raise_for_status()
    vec = r.json()["embedding"]
    row = np.asarray(vec, dtype=np.float32)
    norm = np.linalg.norm(row)
    if norm > 0:
        row = row / norm
    return row


def search_collection(collection_kind: str, query_vec: np.ndarray, top_k: int = 5) -> list[dict]:
    from embed_core.milvus_store import PictureMilvusStore, collection_name

    dim = int(os.environ.get("IMAGE_CLIP_DIM", "1024"))
    store = PictureMilvusStore(collection=collection_name(collection_kind, None), dim=dim)
    try:
        return store.search(query_vec, top_k=top_k)
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:8030"))
    parser.add_argument("--image-object-key", default="oss测试文件/s0000018.jpg")
    parser.add_argument("--video-object-key", default="oss测试文件/工地上作业的工人2.mp4")
    parser.add_argument("--image-media-id", default="s0000018")
    parser.add_argument("--video-media-id", default="gongdi_worker_2")
    parser.add_argument(
        "--query",
        default="工地上作业的工人",
        help="CLIP 文本检索 query",
    )
    parser.add_argument("--skip-ingest", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    base = args.base_url

    if not args.skip_ingest:
        wait_health(base, timeout_s=600)

        print("\n=== 图片 CLIP 入库 ===")
        submit_and_wait(
            base,
            "/v1/images/clip/embed-jobs",
            {
                "object_key": args.image_object_key,
                "content_type": "image/jpeg",
                "media_id": args.image_media_id,
                "return_vector": False,
                "write_vector": True,
            },
        )

        print("\n=== 视频 CLIP 入库（整片 embedding → media_video_clip）===")
        submit_and_wait(
            base,
            "/v1/videos/clip/embed-jobs",
            {
                "object_key": args.video_object_key,
                "content_type": "video/mp4",
                "media_id": args.video_media_id,
                "return_vector": False,
                "write_vector": True,
                "configuration": {
                    "sample_fps": 1.0,
                    "max_frames": 16,
                    "top_k_frames": 4,
                    "segment_seconds": 5,
                },
            },
            timeout_s=3600.0,
        )

    print("\n=== Milvus 检索验证 ===")
    q = encode_query_clip(base, args.query)
    print(f"[query] text={args.query!r} dim={q.size}")

    for kind, label in (("media_image_clip", "图片"), ("media_video_clip", "视频")):
        hits = search_collection(kind, q, top_k=5)
        print(f"\n--- {label} ({kind}) top hits ---")
        if not hits:
            print("  (无结果)")
            continue
        for i, hit in enumerate(hits, 1):
            pk = hit.get("pk") or hit.get("media_id")
            obj = hit.get("object_key") or hit.get("path") or ""
            score = hit.get("score")
            print(f"  {i}. score={score:.4f} pk={pk} object_key={obj}")

    print("\n[done] 若目标 object_key 排在前列，则 Milvus 入库与检索正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
