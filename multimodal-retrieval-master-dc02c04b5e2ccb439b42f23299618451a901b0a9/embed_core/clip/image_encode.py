from __future__ import annotations

import numpy as np
from PIL import Image

from embed_core.clip.embedding import ChineseClipEncoder
from embed_core.vector_utils import l2_normalize


def encode_pil_images(encoder: ChineseClipEncoder, images: list[Image.Image]) -> np.ndarray:
    embeddings, _ = encoder._encode([img.convert("RGB") for img in images], input_key="images")
    return l2_normalize(embeddings)
