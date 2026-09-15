#!/usr/bin/env python3
"""Create embedding-service deploy tarball (run from project root)."""

from __future__ import annotations

import argparse
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INCLUDE = [
    "embed_core",
    "embedding_service",
    "scripts",
    "start_embedding_service.py",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    ".dockerignore",
    ".env.example",
    "DEPLOY.md",
    "README.md",
    "CHANGELOG.md",
    "UPLOAD.md",
    "tests",
    "runtime/tmp/.gitkeep",
    "runtime/logs/.gitkeep",
    "models/.gitkeep",
    "models/MODELS.md",
]

EXCLUDE_DIR_NAMES = {"__pycache__", ".git", ".venv", "venv", "profiles", "artifacts"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".db", ".bin", ".safetensors", ".pt", ".faiss"}


def should_skip(path: Path) -> bool:
    if any(part in EXCLUDE_DIR_NAMES for part in path.parts):
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    return False


def add_path(tar: tarfile.TarFile, rel: str) -> None:
    full = ROOT / rel
    if not full.exists():
        print(f"skip missing: {rel}")
        return
    if full.is_file():
        tar.add(full, arcname=rel)
        return
    for item in full.rglob("*"):
        if item.is_dir():
            continue
        rel_path = item.relative_to(ROOT)
        if should_skip(rel_path):
            continue
        tar.add(item, arcname=str(rel_path).replace("\\", "/"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Pack embedding service for deploy")
    parser.add_argument(
        "-o",
        "--output",
        default="embedding-service.tar.gz",
        help="Output tarball path",
    )
    args = parser.parse_args()

    out = Path(args.output).resolve()
    print(f"Packing to {out}")
    with tarfile.open(out, "w:gz") as tar:
        for rel in INCLUDE:
            add_path(tar, rel)
    print("Done.")
    print("Upload to server, extract, then:")
    print("  pip install -r requirements.txt")
    print("  EMBEDDING_SERVICE_PORT=8030 python start_embedding_service.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
