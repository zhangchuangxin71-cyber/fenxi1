from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from embed_core.clip.embedding import ChineseClipEncoder
from embed_core.clip.image_encode import encode_pil_images

from .image_io import load_representation_frames
from .vector_utils import average_pool_vectors, l2_normalize

__all__ = ["ChineseClipEncoder", "encode_pil_images", "encode_image_path_pooled"]


def encode_image_path_pooled(
    encoder: ChineseClipEncoder, image_path: Path
) -> tuple[np.ndarray, float]:
    frames = load_representation_frames(image_path)
    if len(frames) == 1:
        vector = encode_pil_images(encoder, frames)[0]
        return vector, float(np.linalg.norm(vector))
    frame_vectors = encode_pil_images(encoder, frames)
    vector = average_pool_vectors(frame_vectors)
    return vector, float(np.linalg.norm(vector))
