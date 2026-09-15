"""Preflight checks before loading CLIP / hybrid retrieval engines."""

from __future__ import annotations

from pathlib import Path

from embed_core.milvus_store import (
    PictureMilvusStore,
    hybrid_retrieval_collection,
    media_video_clip_collection,
    milvus_enabled,
)

from .clip.milvus_index import video_clip_dim
from .hybrid.milvus_dense import hybrid_bge_dim
from .profile_paths import ProfileLayout


def _missing(path: Path, label: str) -> str | None:
    if path.exists():
        return None
    return f"{label} 不存在: {path}"


def check_model_path(model_path: Path) -> list[str]:
    issues: list[str] = []
    if not model_path.exists():
        issues.append(f"Chinese-CLIP 模型目录不存在: {model_path}")
        return issues
    has_weight = any(
        model_path.glob(pattern)
        for pattern in ("*.bin", "*.pt", "*.safetensors", "config.json")
    )
    if not has_weight and not any(model_path.iterdir()):
        issues.append(f"模型目录为空: {model_path}")
    return issues


def check_clip_milvus_artifacts(layout: ProfileLayout) -> list[str]:
    del layout
    issues: list[str] = []
    if not milvus_enabled():
        issues.append("VECTOR_STORE 不是 milvus，无法使用 media_video_clip 检索")
        return issues

    collection = media_video_clip_collection(None)
    dim = video_clip_dim()
    try:
        store = PictureMilvusStore(collection=collection, dim=dim)
        count = store.count()
        store.close()
    except Exception as exc:
        issues.append(f"CLIP Milvus 集合不可用 ({collection}): {exc}")
        return issues

    if count == 0:
        issues.append(
            f"CLIP Milvus 集合为空 ({collection})，请先运行 embedding_service "
            "视频 CLIP 任务 write_vector=true"
        )
    return issues


def check_clip_artifacts(layout: ProfileLayout) -> list[str]:
    return check_clip_milvus_artifacts(layout)


def check_hybrid_artifacts(layout: ProfileLayout) -> list[str]:
    issues: list[str] = []
    index_dir = layout.hybrid_index_dir
    if not index_dir.is_dir():
        issues.append(f"Hybrid 索引目录不存在: {index_dir}")
        return issues
    manifest_files = ("embedder_manifest.json", "index_manifest.json")
    if not any((index_dir / name).is_file() for name in manifest_files):
        issues.append(
            f"Hybrid 索引目录缺少 manifest（{', '.join(manifest_files)}）: {index_dir}"
        )
    collection = hybrid_retrieval_collection(layout.hybrid_profile)
    try:
        store = PictureMilvusStore(collection=collection, dim=hybrid_bge_dim())
        count = store.count()
        store.close()
    except Exception as exc:
        issues.append(f"Hybrid Milvus 集合不可用 ({collection}): {exc}")
        count = 0
    if count == 0:
        issues.append(
            f"Hybrid Milvus 集合为空 ({collection})，请先运行 "
            "embedding_service 或其它上游写入 hybrid BGE 稠密向量"
        )
    if msg := _missing(layout.hybrid_metadata_db, "Hybrid metadata.db"):
        issues.append(msg)
    return issues


def run_startup_checks(
    layout: ProfileLayout,
    *,
    model_path: Path,
    need_clip: bool = True,
    need_hybrid: bool = True,
    strict: bool = False,
) -> list[str]:
    """Return human-readable issues. If strict, raise RuntimeError when any issue exists."""
    issues: list[str] = []
    issues.extend(check_model_path(model_path))
    if need_clip:
        issues.extend(check_clip_artifacts(layout))
    if need_hybrid:
        issues.extend(check_hybrid_artifacts(layout))
    if strict and issues:
        raise RuntimeError("启动检查未通过:\n- " + "\n- ".join(issues))
    return issues
