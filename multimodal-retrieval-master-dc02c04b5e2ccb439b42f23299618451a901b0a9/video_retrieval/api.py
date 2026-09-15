"""FastAPI entry for unified video retrieval."""

from __future__ import annotations

import argparse
import mimetypes
from pathlib import Path
from urllib.parse import unquote

from .config import (
    DEFAULT_API_HOST,
    DEFAULT_API_PORT,
    DEFAULT_CLIP_PROFILE,
    DEFAULT_HYBRID_PROFILE,
    DEFAULT_MODEL_PATH,
)
from .profile_paths import resolve_profile_layout
from .schemas import SearchRequest
from .service import VideoSearchService
from .startup_checks import run_startup_checks
from .ui import INDEX_HTML


def create_app(service: VideoSearchService):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse

    app = FastAPI(title="Unified Video Retrieval", version="2.0.0")

    @app.get("/", response_class=HTMLResponse)
    def home():
        return INDEX_HTML

    @app.post("/search")
    def search(req: SearchRequest):
        payload = service.search(req)
        if isinstance(payload, dict) and payload.get("hybrid") is not None:
            # Backward compatibility for clients expecting `doubao` key
            payload["doubao"] = payload["hybrid"]
        return payload

    @app.get("/health")
    def health():
        body = service.health()
        body["doubao_ready"] = body.get("hybrid_ready")
        body["doubao_index_videos"] = body.get("hybrid_index_videos")
        body["doubao_error"] = body.get("hybrid_error")
        body["doubao_starting"] = body.get("hybrid_starting")
        return body

    @app.get("/media/clip/{video_id:path}")
    def media_clip(video_id: str):
        video_id = unquote(video_id).strip()
        path = service.resolve_clip_path(video_id)
        if path is None:
            raise HTTPException(404, detail=f"video not found: {video_id}")
        media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
        return FileResponse(path, media_type=media_type)

    @app.get("/media/hybrid/{video_id:path}")
    @app.get("/media/doubao/{video_id:path}")
    def media_hybrid(video_id: str):
        video_id = unquote(video_id).strip()
        path = service.resolve_hybrid_path(video_id)
        if path is None:
            raise HTTPException(404, detail=f"video not found: {video_id}")
        media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
        return FileResponse(path, media_type=media_type)

    return app


def parse_args():
    p = argparse.ArgumentParser(description="Unified video retrieval API (CLIP + hybrid).")
    p.add_argument(
        "--profile", help="Base profile name for both pipelines when clip/hybrid profiles omitted."
    )
    p.add_argument(
        "--clip-profile",
        default=None,
        help=f"CLIP profile (default: --profile or {DEFAULT_CLIP_PROFILE})",
    )
    p.add_argument(
        "--hybrid-profile",
        "--doubao-profile",
        default=None,
        dest="hybrid_profile",
        help=f"Hybrid profile (default: --profile or {DEFAULT_HYBRID_PROFILE})",
    )
    p.add_argument("--clip-output-dir", type=Path, default=None)
    p.add_argument("--clip-metadata-db", type=Path, default=None)
    p.add_argument("--hybrid-metadata-db", type=Path, default=None)
    p.add_argument("--hybrid-index-dir", type=Path, default=None)
    p.add_argument("--model-path", default=str(DEFAULT_MODEL_PATH))
    p.add_argument("--clip-device", default="cuda")
    p.add_argument("--hybrid-device", default="cuda")
    p.add_argument("--hybrid-batch-size", type=int, default=16)
    p.add_argument("--hybrid-local-files-only", action="store_true")
    p.add_argument("--sparse-top-k", type=int, default=100)
    p.add_argument("--dense-top-k", type=int, default=100)
    p.add_argument("--rrf-k", type=int, default=60)
    p.add_argument(
        "--no-preload-hybrid",
        action="store_true",
        help="Do not load hybrid engine in background at startup.",
    )
    p.add_argument(
        "--no-preload-clip",
        action="store_true",
        help="Do not load CLIP model in background at startup (lazy load on first clip/both search).",
    )
    p.add_argument(
        "--strict-startup",
        action="store_true",
        help="Abort startup when model or index artifacts are missing.",
    )
    p.add_argument(
        "--skip-startup-checks",
        action="store_true",
        help="Skip preflight checks for model and index paths.",
    )
    p.add_argument("--host", default=DEFAULT_API_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_API_PORT)
    return p.parse_args()


def main():
    import uvicorn

    from .env_loader import load_default_dotenv_files

    load_default_dotenv_files()
    args = parse_args()
    clip_profile = args.clip_profile or args.profile or DEFAULT_CLIP_PROFILE
    hybrid_profile = args.hybrid_profile or args.profile or DEFAULT_HYBRID_PROFILE
    layout = resolve_profile_layout(
        args.profile,
        clip_profile=clip_profile,
        hybrid_profile=hybrid_profile,
        clip_output_dir=args.clip_output_dir,
        clip_metadata_db=args.clip_metadata_db,
        hybrid_metadata_db=args.hybrid_metadata_db,
        hybrid_index_dir=args.hybrid_index_dir,
    )
    model_path = Path(args.model_path)
    if not args.skip_startup_checks:
        issues = run_startup_checks(
            layout,
            model_path=model_path,
            need_clip=True,
            need_hybrid=True,
            strict=args.strict_startup,
        )
        for line in issues:
            print(f"[video_retrieval][warn] {line}", flush=True)
    service = VideoSearchService(
        layout,
        model_path=args.model_path,
        clip_device=args.clip_device,
        hybrid_device=args.hybrid_device,
        hybrid_batch_size=args.hybrid_batch_size,
        hybrid_local_files_only=args.hybrid_local_files_only,
        sparse_top_k=args.sparse_top_k,
        dense_top_k=args.dense_top_k,
        rrf_k=args.rrf_k,
        preload_hybrid=not args.no_preload_hybrid,
        preload_clip=not args.no_preload_clip,
    )
    app = create_app(service)
    print(f"[video_retrieval] http://{args.host}:{args.port}/", flush=True)
    print(
        f"[video_retrieval] clip={layout.clip_profile} hybrid={layout.hybrid_profile} unified={layout.unified}",
        flush=True,
    )
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        service.close()


if __name__ == "__main__":
    main()
