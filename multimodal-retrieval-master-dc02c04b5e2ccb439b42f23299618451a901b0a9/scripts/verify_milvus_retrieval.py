#!/usr/bin/env python3
"""Check that picture/ and video_retrieval/ are configured for Milvus retrieval."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except ImportError:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _check_code_paths() -> list[str]:
    issues: list[str] = []
    faiss_files = list(ROOT.glob("**/faiss_store.py"))
    faiss_files = [p for p in faiss_files if ".venv" not in str(p)]
    if faiss_files:
        issues.append(f"FAISS 实现文件仍存在: {faiss_files}")

    picture_retrieval = (ROOT / "picture" / "retrieval.py").read_text(encoding="utf-8")
    if "PictureMilvusStore" not in picture_retrieval or ".milvus_store.search" not in picture_retrieval:
        issues.append("picture/retrieval.py 未使用 Milvus 检索路径")

    clip_factory = (ROOT / "video_retrieval" / "clip" / "retriever_factory.py").read_text(
        encoding="utf-8"
    )
    if "load_media_video_clip_index" not in clip_factory or "FaissFrameIndex" in clip_factory:
        issues.append("video_retrieval CLIP 未接 media_video_clip Milvus 路径")

    hybrid = (ROOT / "video_retrieval" / "hybrid" / "hybrid_retrieval.py").read_text(encoding="utf-8")
    if "self.milvus_store.search" not in hybrid:
        issues.append("video_retrieval Hybrid 稠密路未使用 Milvus")

    return issues


def _collection_rows(
    *,
    clip_profile: str,
    hybrid_profile: str,
    picture_profile: str | None,
) -> list[tuple[str, str, int | str]]:
    from embed_core.milvus_store import (
        PictureMilvusStore,
        collection_name,
        hybrid_retrieval_collection,
        media_video_clip_collection,
        milvus_enabled,
    )

    if not milvus_enabled():
        return [("config", "VECTOR_STORE", os.environ.get("VECTOR_STORE", "(unset) not milvus"))]

    dim_clip = int(os.environ.get("VIDEO_CLIP_DIM", "1024"))
    dim_bge = int(os.environ.get("VIDEO_BGE_DIM", "1024"))
    dim_pic = int(os.environ.get("IMAGE_CLIP_DIM", "1024"))

    del clip_profile
    specs: list[tuple[str, str, int]] = [
        ("media_video_clip", media_video_clip_collection(None), dim_clip),
        ("hybrid_bge", hybrid_retrieval_collection(hybrid_profile), dim_bge),
        ("picture_clip", collection_name("picture_clip", picture_profile), dim_pic),
        ("picture_bge", collection_name("picture_bge", picture_profile), dim_bge),
    ]

    rows: list[tuple[str, str, int | str]] = []
    for kind, collection, dim in specs:
        try:
            store = PictureMilvusStore(collection=collection, dim=dim)
            count = store.count()
            store.close()
            rows.append((kind, collection, count))
        except Exception as exc:
            rows.append((kind, collection, f"ERROR: {exc}"))
    return rows


def _try_load_retrievers(
    *,
    clip_profile: str,
    hybrid_profile: str,
    picture_profile: str | None,
    skip_torch: bool,
) -> list[str]:
    if skip_torch:
        return ["跳过加载检索器（--skip-torch-load）"]

    from embed_core.milvus_store import milvus_enabled
    from video_retrieval.env_loader import load_default_dotenv_files

    load_default_dotenv_files()
    if not milvus_enabled():
        return ["VECTOR_STORE 非 milvus，检索器将拒绝加载"]

    lines: list[str] = []
    try:
        from picture.retrieval import build_retriever as build_picture_clip

        r = build_picture_clip(profile=picture_profile, device="cpu")
        n = r.index_count()
        backend = type(r.milvus_store).__name__
        r.close()
        lines.append(f"picture CLIP: OK backend={backend} count={n}")
    except Exception as exc:
        lines.append(f"picture CLIP: FAIL {exc}")

    try:
        from picture.text_retrieval import build_text_retriever

        t = build_text_retriever(profile=picture_profile, bge_device="cpu")
        n = t.index_count()
        backend = type(t.milvus_store).__name__ if t.milvus_store else "lazy"
        t.close()
        lines.append(f"picture BGE: OK backend={backend} count={n}")
    except Exception as exc:
        lines.append(f"picture BGE: FAIL {exc}")

    try:
        from video_retrieval.profile_paths import resolve_profile_layout
        from video_retrieval.clip.retriever_factory import build_retriever_from_layout

        layout = resolve_profile_layout(clip_profile=clip_profile, hybrid_profile=hybrid_profile)
        # 只验证 Milvus 索引加载，不加载 CLIP 模型（会很慢）
        from video_retrieval.clip.milvus_index import load_media_video_clip_index

        index = load_media_video_clip_index()
        lines.append(f"video CLIP Milvus (media_video_clip): count={index.count()}")
        index.close()
    except Exception as exc:
        lines.append(f"video CLIP Milvus: FAIL {exc}")

    try:
        from video_retrieval.profile_paths import SearchSource, resolve_profile_layout
        from video_retrieval.hybrid.hybrid_retrieval import HybridSearchEngine

        layout = resolve_profile_layout(clip_profile=clip_profile, hybrid_profile=hybrid_profile)
        source = SearchSource(
            name=layout.hybrid_profile,
            metadata_db_path=layout.hybrid_metadata_db,
            index_dir=layout.hybrid_index_dir,
        )
        engine = HybridSearchEngine(
            metadata_db_path=source.metadata_db_path,
            index_dir=source.index_dir,
            collection_profile=source.name,
            embedding_device="cpu",
        )
        dense_n = engine.milvus_store.count()
        engine.close()
        lines.append(f"video Hybrid Milvus dense: OK count={dense_n}")
    except Exception as exc:
        lines.append(f"video Hybrid Milvus dense: FAIL {exc}")

    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clip-profile", default=os.environ.get("CLIP_PROFILE", "apr_media1_project"))
    parser.add_argument("--hybrid-profile", default=os.environ.get("HYBRID_PROFILE", "apr_media1"))
    parser.add_argument("--picture-profile", default=None)
    parser.add_argument("--skip-torch-load", action="store_true", help="Only check env + Milvus counts")
    args = parser.parse_args()

    _load_dotenv()

    from embed_core.milvus_store import milvus_enabled, vector_store_name

    print("=== Milvus 检索验证 ===\n")
    print(f"VECTOR_STORE = {vector_store_name()!r} (milvus_enabled={milvus_enabled()})")
    print(f"MILVUS_URI     = {os.environ.get('MILVUS_URI', '(default http://127.0.0.1:19530)')}")
    print(f"profiles: clip={args.clip_profile} hybrid={args.hybrid_profile} picture={args.picture_profile}\n")

    code_issues = _check_code_paths()
    print("--- 代码路径 ---")
    if code_issues:
        for item in code_issues:
            print(f"  [FAIL] {item}")
    else:
        print("  [OK] picture / video CLIP / video Hybrid 均已接 Milvus，无 faiss_store.py")

    print("\n--- Milvus 集合行数 ---")
    if not milvus_enabled():
        print("  [WARN] VECTOR_STORE 不是 milvus，本地检索不会使用 Milvus")
    else:
        for kind, collection, count in _collection_rows(
            clip_profile=args.clip_profile,
            hybrid_profile=args.hybrid_profile,
            picture_profile=args.picture_profile,
        ):
            label = "OK" if isinstance(count, int) and count > 0 else "WARN"
            print(f"  [{label}] {kind:12} {collection}: {count}")

    print("\n--- 运行时加载（可选）---")
    for line in _try_load_retrievers(
        clip_profile=args.clip_profile,
        hybrid_profile=args.hybrid_profile,
        picture_profile=args.picture_profile,
        skip_torch=args.skip_torch_load,
    ):
        print(f"  {line}")

    print("\n说明:")
    print("  - count=0 表示集合空，需先经 embedding_service 写库（write_vector=true）")
    print("  - 停掉 Milvus 后再检索应报错，可确认未静默回退 FAISS")
    print("  - picture 默认 profile 多为 output 父目录名，可用 --picture-profile 指定")

    failed = bool(code_issues) or not milvus_enabled()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
