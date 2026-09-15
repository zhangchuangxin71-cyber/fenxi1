from __future__ import annotations

import logging
import os
from dataclasses import replace
from pathlib import Path

from .config import ServiceConfig

logger = logging.getLogger(__name__)

DEFAULT_CLIP_REPO = "OFA-Sys/chinese-clip-vit-huge-patch14"
DEFAULT_BGE_REPO = "BAAI/bge-large-zh-v1.5"
DEFAULT_BGE_DIRNAME = "bge-large-zh-v1.5"

CLIP_REQUIRED_FILES = (
    "pytorch_model.bin",
    "config.json",
    "preprocessor_config.json",
    "vocab.txt",
)


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def apply_hf_mirror_if_configured() -> bool:
    """Use HF mirror when HF_USE_MIRROR=1 or HF_ENDPOINT is already set."""
    if os.environ.get("HF_ENDPOINT"):
        return True
    if _env_flag("HF_USE_MIRROR"):
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        logger.info("Using HuggingFace mirror: %s", os.environ["HF_ENDPOINT"])
        return True
    return False


def clip_model_ready(model_dir: Path) -> bool:
    return model_dir.is_dir() and all((model_dir / name).is_file() for name in CLIP_REQUIRED_FILES)


def bge_model_ready(model_dir: Path) -> bool:
    if not model_dir.is_dir() or not (model_dir / "config.json").is_file():
        return False
    weight_files = list(model_dir.glob("*.bin")) + list(model_dir.glob("*.safetensors"))
    return len(weight_files) > 0


def _snapshot_download(repo_id: str, target_dir: Path, *, allow_patterns: list[str] | None) -> None:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "huggingface-hub is required for automatic model download. "
            "Install with: pip install huggingface-hub"
        ) from exc

    target_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading %s -> %s", repo_id, target_dir)
    snapshot_download(
        repo_id=repo_id,
        local_dir=str(target_dir),
        local_dir_use_symlinks=False,
        resume_download=True,
        allow_patterns=allow_patterns,
    )


def download_clip_model(model_dir: Path, *, repo_id: str = DEFAULT_CLIP_REPO) -> None:
    apply_hf_mirror_if_configured()
    _snapshot_download(
        repo_id,
        model_dir,
        allow_patterns=list(CLIP_REQUIRED_FILES),
    )
    if not clip_model_ready(model_dir):
        raise RuntimeError(f"CLIP download finished but required files are missing under {model_dir}")


def download_bge_model(model_dir: Path, *, repo_id: str = DEFAULT_BGE_REPO) -> None:
    apply_hf_mirror_if_configured()
    _snapshot_download(repo_id, model_dir, allow_patterns=None)
    if not bge_model_ready(model_dir):
        raise RuntimeError(f"BGE download finished but required files are missing under {model_dir}")


def resolve_bge_model_dir(config: ServiceConfig) -> Path:
    if config.bge_model_path:
        return Path(config.bge_model_path)
    return Path(config.clip_model_path) / DEFAULT_BGE_DIRNAME


def ensure_models_downloaded(config: ServiceConfig) -> ServiceConfig:
    """
    Download CLIP/BGE weights at startup when local files are missing.
    Returns an updated config (BGE local path is set after a successful download).
    """
    if not config.auto_download_models:
        return config
    if config.bge_local_files_only:
        logger.info("HF_LOCAL_FILES_ONLY is set; skipping automatic model download")
        return config

    clip_dir = Path(config.clip_model_path)
    bge_dir = resolve_bge_model_dir(config)
    clip_repo = os.environ.get("CHINESE_CLIP_MODEL_REPO", DEFAULT_CLIP_REPO)
    bge_repo = config.bge_model_name or DEFAULT_BGE_REPO

    if not clip_model_ready(clip_dir):
        logger.warning("CLIP model not found at %s; downloading from %s", clip_dir, clip_repo)
        download_clip_model(clip_dir, repo_id=clip_repo)
        logger.info("CLIP model ready at %s", clip_dir)
    else:
        logger.info("CLIP model already present at %s", clip_dir)

    bge_model_path = config.bge_model_path
    if not bge_model_ready(bge_dir):
        logger.warning("BGE model not found at %s; downloading from %s", bge_dir, bge_repo)
        download_bge_model(bge_dir, repo_id=bge_repo)
        bge_model_path = str(bge_dir)
        logger.info("BGE model ready at %s", bge_dir)
    else:
        bge_model_path = bge_model_path or str(bge_dir)
        logger.info("BGE model already present at %s", bge_dir)

    return replace(config, bge_model_path=bge_model_path)
