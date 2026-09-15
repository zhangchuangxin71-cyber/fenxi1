from __future__ import annotations

import base64
import io
import re
from pathlib import Path

from PIL import Image

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
ANIMATED_EXTENSIONS = {".gif", ".webp"}
MAX_REPRESENTATION_FRAMES = 3


def iter_images(image_dir: Path):
    for path in sorted(image_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            yield path


def image_id_from_path(image_path: Path, root: Path) -> str:
    return image_path.relative_to(root).with_suffix("").as_posix()


def safe_embedding_filename(image_id: str) -> str:
    return image_id.replace("/", "__").replace("\\", "__")


def _frame_indices(n_frames: int, *, max_frames: int = MAX_REPRESENTATION_FRAMES) -> list[int]:
    if n_frames <= 1:
        return [0]
    if n_frames <= max_frames:
        return list(range(n_frames))
    return sorted({0, n_frames // 2, n_frames - 1})


def load_representation_frames(
    path: Path, *, max_frames: int = MAX_REPRESENTATION_FRAMES
) -> list[Image.Image]:
    with Image.open(path) as img:
        n_frames = int(getattr(img, "n_frames", 1) or 1)
        if n_frames <= 1:
            return [img.convert("RGB")]
        indices = sorted(_frame_indices(n_frames, max_frames=max_frames))[:max_frames]
        frames: list[Image.Image] = []
        for frame_idx in indices:
            img.seek(frame_idx)
            frames.append(img.convert("RGB").copy())
        return frames or [img.convert("RGB")]


def validate_image_decodable(path: Path) -> tuple[bool, str | None, tuple[int, int] | None]:
    try:
        frames = load_representation_frames(path)
        if not frames:
            return False, "no decodable frames", None
        w, h = frames[0].size
        return True, None, (w, h)
    except Exception as exc:
        return False, str(exc), None


def load_image_rgb(source: str | Path) -> Image.Image:
    if isinstance(source, Path):
        return Image.open(source).convert("RGB")
    text = str(source).strip()
    if text.lower().startswith(("http://", "https://")):
        import requests

        response = requests.get(text, timeout=15)
        response.raise_for_status()
        return Image.open(io.BytesIO(response.content)).convert("RGB")
    if Path(text).exists():
        return Image.open(text).convert("RGB")
    if re.match(r"^[\w\-/]+\.\w+$", text):
        return Image.open(resolve_portable_path(text)).convert("RGB")
    raw = base64.b64decode(text, validate=True)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def resolve_portable_path(value: str | Path) -> Path:
    from .portable_paths import resolve_portable_path as _resolve

    return _resolve(value)
