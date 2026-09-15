#!/usr/bin/env python3
"""
Model Download Script for Chinese CLIP Embedding Service

Delegates to embedding_service.model_bootstrap (same logic as service startup).

Usage:
    python scripts/download_models.py [--use-mirror] [--model-dir PATH]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Project root on sys.path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from embedding_service.model_bootstrap import (
    CLIP_REQUIRED_FILES,
    bge_model_ready,
    clip_model_ready,
    download_bge_model,
    download_clip_model,
)


def verify_models(model_dir: Path) -> bool:
    print("\n" + "=" * 60)
    print("Verifying Model Files")
    print("=" * 60)

    all_exist = True
    for filename in CLIP_REQUIRED_FILES:
        filepath = model_dir / filename
        if filepath.exists():
            size_mb = filepath.stat().st_size / (1024 * 1024)
            print(f"✓ {filename} ({size_mb:.2f} MB)")
        else:
            print(f"✗ {filename} (missing)")
            all_exist = False
    return all_exist


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download models for Chinese CLIP Embedding Service"
    )
    parser.add_argument(
        "--use-mirror",
        action="store_true",
        help="Use HF-Mirror for faster downloads (recommended in China)",
    )
    parser.add_argument(
        "--model-dir",
        type=str,
        default=None,
        help="Custom model directory (default: project_root/models)",
    )
    parser.add_argument("--skip-clip", action="store_true", help="Skip Chinese CLIP")
    parser.add_argument("--skip-bge", action="store_true", help="Skip BGE")

    args = parser.parse_args()

    if args.use_mirror:
        os.environ["HF_USE_MIRROR"] = "1"

    model_dir = Path(args.model_dir).resolve() if args.model_dir else (_ROOT / "models").resolve()
    model_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Chinese CLIP Embedding Service - Model Downloader")
    print("=" * 60)
    print(f"Model directory: {model_dir}")
    print(f"Use mirror: {args.use_mirror}")
    print()

    success = True

    if not args.skip_clip:
        if clip_model_ready(model_dir):
            print("⊘ Chinese CLIP already present, skipping download")
        else:
            try:
                download_clip_model(model_dir)
                print("✓ Chinese CLIP model downloaded successfully!")
            except Exception as exc:
                print(f"❌ Error downloading Chinese CLIP: {exc}")
                success = False
    else:
        print("\n⊘ Skipping Chinese CLIP download")

    bge_dir = model_dir / "bge-large-zh-v1.5"
    if not args.skip_bge:
        if bge_model_ready(bge_dir):
            print("⊘ BGE already present, skipping download")
        else:
            try:
                download_bge_model(bge_dir)
                print("✓ BGE model downloaded successfully!")
            except Exception as exc:
                print(f"❌ Error downloading BGE: {exc}")
                success = False
    else:
        print("\n⊘ Skipping BGE download")

    if not args.skip_clip and not verify_models(model_dir):
        success = False

    print("\n" + "=" * 60)
    if success:
        print("✓ All models downloaded successfully!")
        print("\nNext steps:")
        print('1. Start: $env:EMBEDDING_SERVICE_PORT="8030"; python start_embedding_service.py')
        print("2. Open docs: http://127.0.0.1:8030/docs")
        print("3. Health:  curl http://127.0.0.1:8030/health")
    else:
        print("❌ Some downloads failed. Please check the errors above.")
        return 1

    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
