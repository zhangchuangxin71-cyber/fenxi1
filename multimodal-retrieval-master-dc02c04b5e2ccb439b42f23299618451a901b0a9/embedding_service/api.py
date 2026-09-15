from __future__ import annotations

import asyncio
import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from embed_core.clip.frame_selection import select_diverse_frame_indices
from embed_core.clip.segment_embedding import softmax_attention_pooling
from embed_core.clip.segmenter import segment_video
from embed_core.clip.video_representation import (
    compute_frame_diff_motion_score,
    compute_segment_importance,
)
from embed_core.milvus_store import PictureMilvusStore, collection_name, milvus_enabled

from .config import ServiceConfig
from .launch import print_startup_urls
from .errors import EmbeddingServiceError, TaskLimitError, ValidationError, VectorStoreError
from .logging_config import setup_logging
from .media_fetcher import load_image
from .model_registry import ModelRegistry, encode_bge_text, encode_clip_text, encode_image_clip
from .schemas import (
    DualQueryEmbedRequest,
    HealthResponse,
    ImageBgeEmbedRequest,
    ImageBgeEmbedResponse,
    ImageClipEmbedRequest,
    ImageClipEmbedResponse,
    MediaMetadata,
    QueryVectorPayload,
    VideoBgeEmbedRequest,
    VideoBgeEmbedResponse,
    VideoClipEmbedRequest,
    VideoClipEmbedResponse,
)
from .runtime_cleanup import run_runtime_cleanup, start_periodic_cleanup
from .tasks import TaskManager, task_manager
from .user_errors import (
    REASON_EMPTY_DESCRIPTION,
    REASON_MILVUS_NOT_ENABLED,
    REASON_NO_EMBEDDINGS,
    REASON_NO_SEGMENTS,
    format_exception,
    format_service_error,
)
from .video_utils import cleanup_video_workspace, load_video

logger = logging.getLogger(__name__)

# Initialize configuration
from .model_bootstrap import ensure_models_downloaded

config = ServiceConfig.from_env()
config = ensure_models_downloaded(config)
config.validate()

# Setup logging (console only; no embedding_service.log file)
setup_logging(level=config.log_level)

# Initialize task manager with config
task_manager = TaskManager(
    db_path=config.task_db_path,
    max_tasks=config.max_tasks,
    ttl_hours=config.task_ttl_hours,
)

# Initialize model registry
registry = ModelRegistry(config)

# Thread pool for async tasks
# Use configured worker count from config
executor = ThreadPoolExecutor(
    max_workers=config.embedding_workers,
    thread_name_prefix="EmbedWorker",
)

# FastAPI app
app = FastAPI(
    title="媒体与文本向量服务",
    version="0.3.0",
    description="""
基于 Chinese-CLIP 与 BGE 的媒体、文本向量编码服务。

## 快速开始

### 文本查询（同步，立即返回）
- **CLIP**：`POST /v1/query/clip/embed` — 中文查询文本 → CLIP 向量
- **BGE**：`POST /v1/query/bge/embed` — 中文查询文本 → BGE 向量

### 图片向量（异步，CLIP / BGE 各一组）
- CLIP：`POST/GET /v1/images/clip/embed-jobs[/{job_id}[/result]]`
- BGE：`POST/GET /v1/images/bge/embed-jobs[/{job_id}[/result]]`

### 视频向量（异步，CLIP / BGE 各一组）
- CLIP：`POST/GET /v1/videos/clip/embed-jobs[/{job_id}[/result]]`
- BGE：`POST/GET /v1/videos/bge/embed-jobs[/{job_id}[/result]]`

## 特性
- 文本接口同步返回，无需轮询
- 高分辨率视频自动缩放后处理
- 视频 CLIP：段内 K-means 选帧 → 段内/全片 softmax 池化，仅返回整片向量
- 启动时预加载模型并对 CLIP/BGE 文本分支预热；异步任务持久化到 SQLite

参数说明详见仓库内 `embedding_service/PARAMS.md`。
    """,
    swagger_ui_parameters={
        # 隐藏页面底部 Schemas 区域；各接口 Try it out 仍可用
        "defaultModelsExpandDepth": -1,
        "defaultModelExpandDepth": 0,
    },
    redoc_url=None,
)


def _should_skip_perf_log(path: str, method: str) -> bool:
    """Skip high-frequency poll/health paths to avoid log blow-up on batch jobs."""
    if path in {"/health", "/stats"}:
        return True
    if method == "GET" and "/embed-jobs/" in path and not path.endswith("/result"):
        return True
    return False


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log request timing (excluding job poll and health checks)."""
    import time
    path = request.url.path
    skip = _should_skip_perf_log(path, request.method)
    start_time = time.perf_counter()
    if not skip:
        logger.info(f"[PERF] Request received: {request.method} {path}")
    response = await call_next(request)
    if not skip:
        process_time = (time.perf_counter() - start_time) * 1000
        logger.info(f"[PERF] Request completed in {process_time:.2f}ms")
    return response


@app.exception_handler(EmbeddingServiceError)
async def embedding_service_error_handler(request: Request, exc: EmbeddingServiceError):
    """Handle custom service errors."""
    payload = format_service_error(exc)
    logger.error(
        f"Service error: {exc.code} - {exc.message}",
        extra={"details": exc.details, "user_error": payload["error"]},
    )
    return JSONResponse(
        status_code=400,
        content=payload,
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError):
    """Return Chinese validation errors for malformed requests."""
    return JSONResponse(
        status_code=422,
        content={
            "error": "请求参数无效，请检查必填字段与格式",
            "code": "VALIDATION_ERROR",
            "details": {"fields": exc.errors()},
        },
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle unexpected errors."""
    logger.exception(f"Unexpected error: {exc}")
    payload = format_exception(exc)
    if config.log_level == "DEBUG":
        payload["details"]["debug_message"] = str(exc)
    return JSONResponse(
        status_code=500,
        content=payload,
    )


@app.on_event("startup")
async def startup_event():
    """Service startup tasks."""
    logger.info("=" * 60)
    logger.info("Starting Media Embedding Service")
    logger.info(f"Version: 0.3.0")
    logger.info(f"CLIP Model: {config.clip_model_path}")
    logger.info(f"CLIP Device: {config.clip_device}")
    logger.info(f"BGE Model: {config.bge_model_name}")
    logger.info(f"BGE Device: {config.bge_device}")
    logger.info(f"Task DB: {config.task_db_path}")
    logger.info(f"Max Tasks: {config.max_tasks}")
    if config.oss_configured():
        logger.info(f"OSS: bucket={config.aliyun_oss_bucket}, endpoint={config.aliyun_oss_endpoint}")
    else:
        missing = ", ".join(config.missing_oss_env_names())
        logger.warning(
            "OSS not configured (%s). Image/video CLIP jobs with object_key will fail "
            "until .env or environment variables are set.",
            missing,
        )
    logger.info("=" * 60)

    cleanup_stats = run_runtime_cleanup(config, task_manager, on_startup=True)
    if any(
        (
            cleanup_stats.interrupted_jobs,
            cleanup_stats.tmp_entries_removed,
            cleanup_stats.log_files_removed,
        )
    ):
        logger.info(
            "Startup runtime cleanup: interrupted_jobs=%s tmp_removed=%s tmp_freed_mb=%.1f "
            "log_removed=%s log_freed_mb=%.1f",
            cleanup_stats.interrupted_jobs,
            cleanup_stats.tmp_entries_removed,
            cleanup_stats.tmp_bytes_freed / (1024 * 1024),
            cleanup_stats.log_files_removed,
            cleanup_stats.log_bytes_freed / (1024 * 1024),
        )
    start_periodic_cleanup(config, task_manager)
    
    # Preload models to avoid slow first request
    logger.info("Preloading models...")
    try:
        logger.info("Loading CLIP model...")
        registry.get_clip_encoder()
        logger.info("✓ CLIP model loaded")
    except Exception as exc:
        logger.warning(f"Failed to preload CLIP model: {exc}")
    
    try:
        logger.info("Loading BGE model...")
        registry.get_bge_embedder()
        logger.info("✓ BGE model loaded")
    except Exception as exc:
        logger.warning(f"Failed to preload BGE model: {exc}")

    warmup_text = "预热"
    logger.info("Warming up text encoders (first inference)...")
    import time

    if registry.is_clip_loaded():
        try:
            t0 = time.perf_counter()
            encode_clip_text(registry, warmup_text)
            logger.info(f"✓ CLIP text warmup done in {(time.perf_counter() - t0) * 1000:.0f}ms")
        except Exception as exc:
            logger.warning(f"CLIP text warmup failed: {exc}")

    if registry.is_bge_loaded():
        try:
            t0 = time.perf_counter()
            encode_bge_text(registry, warmup_text)
            logger.info(f"✓ BGE text warmup done in {(time.perf_counter() - t0) * 1000:.0f}ms")
        except Exception as exc:
            logger.warning(f"BGE text warmup failed: {exc}")
    
    logger.info("=" * 60)
    logger.info("Service ready!")
    logger.info("=" * 60)

    async def _print_urls_when_ready() -> None:
        await asyncio.sleep(0.2)
        print()
        print_startup_urls(config.service_port)

    asyncio.create_task(_print_urls_when_ready())


@app.on_event("shutdown")
async def shutdown_event():
    """Service shutdown tasks."""
    logger.info("Shutting down Media Embedding Service")
    executor.shutdown(wait=True, cancel_futures=False)
    task_manager.close()
    logger.info("Service shutdown complete")


def _write_milvus(collection_kind: str, pk: str, dim: int, payload: dict[str, Any]) -> dict:
    """Write vector to Milvus with error handling."""
    if not milvus_enabled():
        raise VectorStoreError(
            "write_vector=true requires VECTOR_STORE=milvus",
            details={"reason": REASON_MILVUS_NOT_ENABLED},
        )
    try:
        store = PictureMilvusStore(collection=collection_name(collection_kind, None), dim=dim)
        store.upsert([{"pk": pk, **payload}])
        info = {"collection": store.collection, "pk": pk, "written": True}
        store.close()
        logger.info(f"Written to Milvus: {collection_kind}/{pk}")
        return info
    except Exception as exc:
        logger.error(f"Milvus write failed: {exc}")
        raise VectorStoreError(
            f"Milvus write failed: {exc}",
            details={"reason": "MILVUS_ERROR"},
        ) from exc


def _embed_image_clip_inner(req: ImageClipEmbedRequest) -> ImageClipEmbedResponse:
    """Process image CLIP embedding."""
    logger.info(f"Processing image CLIP embedding: {req.media_id}")
    loaded = load_image(req, config)
    embedding, model_name = encode_image_clip(registry, loaded.image)
    milvus_info = None
    if req.write_vector:
        milvus_info = _write_milvus(
            "media_image_clip",
            f"image_clip:image:{req.media_id}",
            len(embedding),
            {
                "media_id": req.media_id,
                "image_id": req.media_id,
                "path": loaded.object_key or req.media_id,
                "bucket": loaded.bucket or "",
                "object_key": loaded.object_key,
                "width": loaded.image.width,
                "height": loaded.image.height,
                "model_name": model_name,
                "modality": "image",
                "scheme": "clip",
                "content_type": req.content_type,
                "vector": embedding,
            },
        )
    logger.info(f"Completed image CLIP embedding: {req.media_id}")
    return ImageClipEmbedResponse(
        media_id=req.media_id,
        vector_dim=len(embedding),
        embedding_model=model_name,
        embedding=embedding if req.return_vector else None,
        metadata=MediaMetadata(
            width=loaded.image.width,
            height=loaded.image.height,
            source=loaded.source,
            content_type=loaded.content_type,
            bucket=loaded.bucket,
            object_key=loaded.object_key,
        ),
        milvus=milvus_info,
    )


def _embed_image_bge_inner(req: ImageBgeEmbedRequest) -> ImageBgeEmbedResponse:
    """Process image BGE embedding."""
    logger.info(f"Processing image BGE embedding: {req.media_id}")
    text = req.description.strip()
    if not text:
        raise ValidationError(
            "empty image description",
            details={"reason": REASON_EMPTY_DESCRIPTION},
        )
    embedding, model_name = encode_bge_text(registry, text)
    milvus_info = None
    if req.write_vector:
        milvus_info = _write_milvus(
            "media_image_bge",
            f"image_bge:image:{req.media_id}",
            len(embedding),
            {
                "media_id": req.media_id,
                "image_id": req.media_id,
                "path": req.media_id,
                "bucket": "",
                "object_key": "",
                "caption_text": text,
                "model_name": model_name,
                "modality": "image",
                "scheme": "bge",
                "content_type": req.content_type,
                "vector": embedding,
            },
        )
    logger.info(f"Completed image BGE embedding: {req.media_id}")
    return ImageBgeEmbedResponse(
        media_id=req.media_id,
        vector_dim=len(embedding),
        embedding_model=model_name,
        caption=text,
        embedding=embedding if req.return_vector else None,
        metadata=MediaMetadata(
            width=None,
            height=None,
            content_type=req.content_type,
            bucket=None,
            object_key=None,
        ),
        milvus=milvus_info,
    )


def _embed_video_clip_inner(req: VideoClipEmbedRequest) -> VideoClipEmbedResponse:
    """Process video CLIP: segment → sample → K-means frames → pool to video vector."""
    cfg = req.configuration
    logger.info(f"Processing video CLIP embedding: {req.media_id}")
    loaded = load_video(req, config)
    segments_dir = loaded.path.parent / f"{loaded.path.stem}_segments"
    segment_count = 0
    selected_frame_count = 0
    try:
        logger.debug(f"Segmenting video: {req.media_id}")
        segment_paths = segment_video(
            loaded.path,
            segments_dir,
            cfg.segment_seconds,
            max_resolution=config.max_video_resolution,
        )
        if not segment_paths:
            raise ValidationError(
                "no video segments generated",
                details={"reason": REASON_NO_SEGMENTS},
            )

        encoder = registry.get_clip_encoder()
        segment_vectors: list[np.ndarray] = []
        segment_importance_scores: list[float] = []

        total_segments = len(segment_paths)
        logger.info(f"Processing {total_segments} segments for {req.media_id}")

        for idx, segment_path in enumerate(segment_paths):
            logger.debug(f"Processing segment {idx + 1}/{total_segments}: {segment_path.name}")
            capture = cv2.VideoCapture(str(segment_path))
            if not capture.isOpened():
                logger.warning(f"Failed to open segment: {segment_path}")
                continue

            fps = capture.get(cv2.CAP_PROP_FPS) or 0.0
            frame_step = max(1, int(round(fps / cfg.sample_fps))) if fps > 0 else 1
            frame_paths: list[Path] = []
            frame_timestamps: list[float] = []
            frame_index = -1
            try:
                while len(frame_paths) < cfg.max_frames:
                    ok, frame = capture.read()
                    if not ok:
                        break
                    frame_index += 1
                    if frame_index % frame_step != 0:
                        continue

                    height, width = frame.shape[:2]
                    max_dim = max(height, width)
                    if max_dim > config.max_frame_resolution:
                        scale = config.max_frame_resolution / max_dim
                        new_width = int(width * scale)
                        new_height = int(height * scale)
                        frame = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)

                    frame_path = segments_dir / f"{segment_path.stem}_f{frame_index:06d}.jpg"
                    if not cv2.imwrite(str(frame_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 85]):
                        raise RuntimeError(f"Failed to write frame: {frame_path}")
                    frame_paths.append(frame_path)
                    frame_timestamps.append(frame_index / fps if fps > 0 else 0.0)
            finally:
                capture.release()

            if not frame_paths:
                logger.warning(f"No frames extracted from segment {idx}")
                continue

            candidate_vectors = encoder.encode_images([str(path) for path in frame_paths]).embeddings
            if candidate_vectors.size == 0:
                logger.warning(f"No embeddings generated for segment {idx}")
                continue

            selected_idx = select_diverse_frame_indices(
                candidate_vectors,
                top_k=cfg.top_k_frames,
            )
            frame_vectors = candidate_vectors[selected_idx]
            selected_paths = [frame_paths[int(i)] for i in selected_idx]
            selected_timestamps = [frame_timestamps[int(i)] for i in selected_idx]
            selected_frame_count += int(frame_vectors.shape[0])

            frame_norms = np.linalg.norm(frame_vectors.astype(np.float32), axis=1)
            pooled_vec, _ = softmax_attention_pooling(frame_vectors, frame_norms)

            frame_motion = compute_frame_diff_motion_score([str(p) for p in selected_paths])
            segment_duration = cfg.segment_seconds if cfg.segment_seconds > 0 else None
            importance_score, _, _, _ = compute_segment_importance(
                frame_embeddings=frame_vectors,
                frame_norms=frame_norms,
                frame_timestamps=np.asarray(selected_timestamps, dtype=np.float32),
                segment_duration_seconds=segment_duration,
                frame_diff_motion_score=frame_motion,
                embedding_norm_weight=0.35,
                motion_score_weight=0.35,
                visual_diversity_weight=0.30,
            )

            segment_vectors.append(pooled_vec)
            segment_importance_scores.append(float(importance_score))
            segment_count += 1

            for frame_path in frame_paths:
                try:
                    frame_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.debug("Failed to delete frame %s: %s", frame_path, exc)
            try:
                segment_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("Failed to delete segment %s: %s", segment_path, exc)

        if not segment_vectors:
            raise ValidationError(
                "no segment embeddings generated",
                details={"reason": REASON_NO_EMBEDDINGS},
            )

        segment_matrix = np.vstack(segment_vectors).astype(np.float32)
        importance_weights = np.asarray(segment_importance_scores, dtype=np.float32)
        video_vec, _ = softmax_attention_pooling(segment_matrix, importance_weights)
        video_embedding = video_vec.astype(np.float32).tolist()
        model_name = encoder.model_name
    finally:
        cleanup_errors = cleanup_video_workspace(loaded.path)
        if cleanup_errors:
            logger.warning(f"Video cleanup had {len(cleanup_errors)} errors: {cleanup_errors}")
        try:
            if Path(segments_dir).exists():
                shutil.rmtree(segments_dir)
                logger.debug(f"Cleaned up segments directory: {segments_dir}")
        except Exception as exc:
            logger.warning(f"Failed to clean up segments directory: {exc}")

    milvus_info = None
    if req.write_vector:
        milvus_info = _write_milvus(
            "media_video_clip",
            f"video_clip:video:{req.media_id}",
            len(video_embedding),
            {
                "media_id": req.media_id,
                "path": loaded.object_key or req.media_id,
                "bucket": loaded.bucket or "",
                "object_key": loaded.object_key,
                "model_name": model_name,
                "modality": "video",
                "scheme": "clip",
                "content_type": req.content_type,
                "segment_count": segment_count,
                "frame_count": selected_frame_count,
                "vector": video_embedding,
            },
        )

    logger.info(
        f"Completed video CLIP embedding: {req.media_id} "
        f"({segment_count} segments, {selected_frame_count} selected frames)"
    )
    return VideoClipEmbedResponse(
        media_id=req.media_id,
        vector_dim=len(video_embedding),
        embedding_model=model_name,
        embedding=video_embedding if req.return_vector else None,
        metadata=MediaMetadata(
            width=None,
            height=None,
            source=loaded.source,
            content_type=loaded.content_type,
            bucket=loaded.bucket,
            object_key=loaded.object_key,
        ),
        milvus=milvus_info,
    )


def _embed_video_bge_inner(req: VideoBgeEmbedRequest) -> VideoBgeEmbedResponse:
    """Process video BGE embedding."""
    logger.info(f"Processing video BGE embedding: {req.media_id}")
    video_caption = req.description.strip()
    if not video_caption:
        raise ValidationError(
            "empty video description",
            details={"reason": REASON_EMPTY_DESCRIPTION},
        )
    embedding, model_name = encode_bge_text(registry, video_caption)
    milvus_info = None
    if req.write_vector:
        milvus_info = _write_milvus(
            "media_video_bge",
            f"video_bge:video:{req.media_id}",
            len(embedding),
            {
                "media_id": req.media_id,
                "path": req.media_id,
                "bucket": "",
                "object_key": "",
                "video_caption": video_caption,
                "model_name": model_name,
                "modality": "video",
                "scheme": "bge",
                "content_type": req.content_type,
                "vector": embedding,
            },
        )
    logger.info(f"Completed video BGE embedding: {req.media_id}")
    return VideoBgeEmbedResponse(
        media_id=req.media_id,
        vector_dim=len(embedding),
        embedding_model=model_name,
        caption=video_caption,
        embedding=embedding if req.return_vector else None,
        segments=[],
        metadata=MediaMetadata(
            width=None,
            height=None,
            content_type=req.content_type,
            bucket=None,
            object_key=None,
        ),
        milvus=milvus_info,
    )


@app.get("/health", response_model=HealthResponse, tags=["系统"])
async def health() -> HealthResponse:
    """健康检查：返回向量库模式、模型路径与设备信息（async 避免 Docker 重负载时阻塞线程池）。"""
    return HealthResponse(
        vector_store=os.environ.get("VECTOR_STORE", "milvus"),
        clip_model_path=config.clip_model_path,
        clip_device=config.clip_device,
        bge_model_name=config.bge_model_name,
        bge_device=config.bge_device,
    )


@app.get("/stats", tags=["系统"])
def get_stats():
    """
    服务统计与监控信息。

    返回字段包括：
    - **version**：服务版本
    - **tasks**：任务数量（total / pending / running / succeeded / failed）
    - **models**：CLIP、BGE 是否已加载到内存
    """
    task_stats = task_manager.get_stats()
    return {
        "service": "embedding_service",
        "version": "0.3.0",
        "tasks": task_stats,
        "models": {
            "clip_loaded": registry.is_clip_loaded(),
            "bge_loaded": registry.is_bge_loaded(),
        },
    }


def _embed_query_clip_inner(req: DualQueryEmbedRequest) -> QueryVectorPayload:
    """Process query CLIP embedding."""
    import time
    start = time.perf_counter()
    clip_embedding, clip_model = encode_clip_text(registry, req.text)
    elapsed = (time.perf_counter() - start) * 1000
    logger.info(f"CLIP encoding took {elapsed:.2f}ms")
    return QueryVectorPayload(
        embedding_model=clip_model,
        embedding=clip_embedding if req.return_vector else None,
        vector_dim=len(clip_embedding),
    )


def _embed_query_bge_inner(req: DualQueryEmbedRequest) -> QueryVectorPayload:
    """Process query BGE embedding."""
    bge_embedding, bge_model = encode_bge_text(registry, req.text)
    return QueryVectorPayload(
        embedding_model=bge_model,
        embedding=bge_embedding if req.return_vector else None,
        vector_dim=len(bge_embedding),
    )


def _submit_job(worker, payload, job_kind: str):
    """Submit job to thread pool executor."""
    try:
        record = task_manager.create(job_kind)
    except RuntimeError as exc:
        raise TaskLimitError(str(exc), details={"reason": "TASK_LIMIT", "max_tasks": config.max_tasks})

    media_id = getattr(payload, "media_id", None)
    object_key = getattr(payload, "object_key", None)

    def _run():
        task_manager.update(record.job_id, status="running", progress={"stage": "started"})
        try:
            result = worker(payload)
            task_manager.update(record.job_id, status="succeeded", result=result.model_dump())
        except EmbeddingServiceError as exc:
            error_payload = format_service_error(exc)
            logger.error(
                "Task %s failed: %s - %s (media_id=%s, object_key=%s, user_error=%s)",
                record.job_id,
                exc.code,
                exc.message,
                media_id,
                object_key,
                error_payload["error"],
            )
            task_manager.update(
                record.job_id,
                status="failed",
                error=error_payload["error"],
                error_type=error_payload["code"],
                error_details=error_payload["details"],
            )
        except Exception as exc:
            error_payload = format_exception(exc)
            logger.exception(
                "Task %s failed with unexpected error (media_id=%s, object_key=%s, user_error=%s)",
                record.job_id,
                media_id,
                object_key,
                error_payload["error"],
            )
            task_manager.update_error(
                record.job_id,
                exc,
                error=error_payload["error"],
                error_type=error_payload["code"],
                error_details=error_payload["details"],
            )

    executor.submit(_run)
    logger.info("Submitted job: %s (media_id=%s, object_key=%s, job_kind=%s)", record.job_id, media_id, object_key, job_kind)
    return {"job_id": record.job_id, "status": record.status}


def _job_matches_scheme(
    job_kind: str,
    modality: Literal["image", "video"],
    scheme: Literal["clip", "bge"],
) -> bool:
    if not job_kind:
        return True
    prefixes = (f"{modality}/{scheme}", f"{modality}s/{scheme}")
    return job_kind.startswith(prefixes)


def _get_job(
    job_id: str,
    modality: Literal["image", "video"],
    scheme: Literal["clip", "bge"],
    jobs_path: str,
) -> dict:
    record = task_manager.get(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="任务不存在")
    if record.job_kind and not _job_matches_scheme(record.job_kind, modality, scheme):
        raise HTTPException(status_code=404, detail="任务不存在")

    response = {
        "job_id": record.job_id,
        "job_kind": record.job_kind,
        "status": record.status,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "progress": record.progress,
        "error": record.error,
        "error_type": record.error_type,
    }
    if record.status == "failed" and record.error_details:
        response["error_details"] = record.error_details
    if record.status == "succeeded" and record.result:
        response["result_size_bytes"] = record.result_size_bytes
        response["result_url"] = f"{jobs_path}/{job_id}/result"
    return response


def _get_job_result(
    job_id: str,
    modality: Literal["image", "video"],
    scheme: Literal["clip", "bge"],
) -> dict:
    record = task_manager.get(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="任务不存在")
    if record.job_kind and not _job_matches_scheme(record.job_kind, modality, scheme):
        raise HTTPException(status_code=404, detail="任务不存在")
    if record.status != "succeeded":
        raise HTTPException(
            status_code=409,
            detail=f"任务尚未完成（当前状态：{record.status}），暂无法获取结果",
        )
    return record.result


@app.post("/v1/images/clip/embed-jobs", tags=["图片 CLIP"])
def create_image_clip_job(req: ImageClipEmbedRequest):
    """图片 CLIP 向量（异步）。需 `object_key`（OSS，桶名见环境变量 `ALIYUN_OSS_BUCKET`）。"""
    return _submit_job(_embed_image_clip_inner, req, "image/clip")


@app.post("/v1/images/bge/embed-jobs", tags=["图片 BGE"])
def create_image_bge_job(req: ImageBgeEmbedRequest):
    """图片 BGE 向量（异步）。对文字描述编码，需 `description`。"""
    return _submit_job(_embed_image_bge_inner, req, "image/bge")


@app.post("/v1/videos/clip/embed-jobs", tags=["视频 CLIP"])
def create_video_clip_job(req: VideoClipEmbedRequest):
    """视频 CLIP 向量（异步）。需 `object_key`；分段抽帧、K-means 选帧后仅返回整片 embedding。"""
    return _submit_job(_embed_video_clip_inner, req, "video/clip")


@app.post("/v1/videos/bge/embed-jobs", tags=["视频 BGE"])
def create_video_bge_job(req: VideoBgeEmbedRequest):
    """视频 BGE 向量（异步）。对视频文字描述编码。"""
    return _submit_job(_embed_video_bge_inner, req, "video/bge")


@app.post("/v1/query/clip/embed", tags=["文本向量"])
def embed_query_clip(req: DualQueryEmbedRequest) -> QueryVectorPayload:
    """
    文本 CLIP 向量（同步，立即返回）。

    适用于搜索查询、实时交互；模型已加载时通常数百毫秒内返回。
    """
    import time
    endpoint_start = time.perf_counter()
    logger.info(f"[PERF] Endpoint started")
    result = _embed_query_clip_inner(req)
    logger.info(f"[PERF] Model encoding completed")
    endpoint_elapsed = (time.perf_counter() - endpoint_start) * 1000
    logger.info(f"[PERF] Total endpoint time: {endpoint_elapsed:.2f}ms")
    return result


@app.post("/v1/query/bge/embed", tags=["文本向量"])
def embed_query_bge(req: DualQueryEmbedRequest) -> QueryVectorPayload:
    """
    文本 BGE 向量（同步，立即返回）。

    适用于搜索查询、实时交互；模型已加载时通常数百毫秒内返回。
    """
    return _embed_query_bge_inner(req)


@app.get("/v1/images/clip/embed-jobs/{job_id}", tags=["图片 CLIP"])
def get_image_clip_job(job_id: str):
    return _get_job(job_id, "image", "clip", "/v1/images/clip/embed-jobs")


@app.get("/v1/images/clip/embed-jobs/{job_id}/result", tags=["图片 CLIP"])
def get_image_clip_job_result(job_id: str):
    return _get_job_result(job_id, "image", "clip")


@app.get("/v1/images/bge/embed-jobs/{job_id}", tags=["图片 BGE"])
def get_image_bge_job(job_id: str):
    return _get_job(job_id, "image", "bge", "/v1/images/bge/embed-jobs")


@app.get("/v1/images/bge/embed-jobs/{job_id}/result", tags=["图片 BGE"])
def get_image_bge_job_result(job_id: str):
    return _get_job_result(job_id, "image", "bge")


@app.get("/v1/videos/clip/embed-jobs/{job_id}", tags=["视频 CLIP"])
def get_video_clip_job(job_id: str):
    return _get_job(job_id, "video", "clip", "/v1/videos/clip/embed-jobs")


@app.get("/v1/videos/clip/embed-jobs/{job_id}/result", tags=["视频 CLIP"])
def get_video_clip_job_result(job_id: str):
    """取视频 CLIP 任务结果（仅整片 embedding 向量）。"""
    return _get_job_result(job_id, "video", "clip")


@app.get("/v1/videos/bge/embed-jobs/{job_id}", tags=["视频 BGE"])
def get_video_bge_job(job_id: str):
    return _get_job(job_id, "video", "bge", "/v1/videos/bge/embed-jobs")


@app.get("/v1/videos/bge/embed-jobs/{job_id}/result", tags=["视频 BGE"])
def get_video_bge_job_result(job_id: str):
    return _get_job_result(job_id, "video", "bge")


if __name__ == "__main__":
    from .launch import run_server

    logger.info(f"Starting server on {config.service_host}:{config.service_port}")
    run_server(
        app,
        host=config.service_host,
        port=config.service_port,
        log_level=config.log_level,
        workers=config.workers,
    )
