from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from transformers import ChineseCLIPModel, ChineseCLIPProcessor


def _ensure_feature_tensor(features) -> torch.Tensor:
    if isinstance(features, torch.Tensor):
        return features
    if hasattr(features, "text_embeds") and features.text_embeds is not None:
        return features.text_embeds
    if hasattr(features, "image_embeds") and features.image_embeds is not None:
        return features.image_embeds
    if hasattr(features, "pooler_output") and features.pooler_output is not None:
        return features.pooler_output
    if hasattr(features, "last_hidden_state") and features.last_hidden_state is not None:
        return features.last_hidden_state[:, 0]
    raise TypeError(f"Unsupported feature output type: {type(features)!r}")


def _normalize(features: torch.Tensor) -> torch.Tensor:
    return features / features.norm(dim=-1, keepdim=True).clamp_min(1e-12)


@dataclass
class EncodedBatch:
    embeddings: np.ndarray
    norms: np.ndarray


class ChineseClipEncoder:
    def __init__(self, model_path: str, device: str = "cuda", batch_size: int = 32):
        if device == "cuda" and not torch.cuda.is_available():
            device = "cpu"
        self.model_path = model_path
        self.device = torch.device(device)
        self.batch_size = batch_size
        self.processor = ChineseCLIPProcessor.from_pretrained(model_path, local_files_only=True)
        self.model = ChineseCLIPModel.from_pretrained(model_path, local_files_only=True).to(
            self.device
        )
        self.model.eval()

    @property
    def model_name(self) -> str:
        config_name = getattr(self.model.config, "name_or_path", "") or ""
        if config_name:
            return str(config_name)
        return Path(self.model_path).name

    @property
    def model_source_path(self) -> str:
        return str(Path(self.model_path).resolve())

    @property
    def model_revision(self) -> str:
        commit_hash = getattr(self.model.config, "_commit_hash", None)
        if commit_hash:
            return str(commit_hash)
        transformers_version = getattr(self.model.config, "transformers_version", None)
        if transformers_version:
            return f"transformers-{transformers_version}"
        return "unknown"

    @property
    def embedding_dtype(self) -> str:
        return str(np.dtype(np.float32))

    @property
    def embedding_dim(self) -> int:
        projection_dim = getattr(self.model.config, "projection_dim", None)
        if projection_dim is not None:
            return int(projection_dim)
        visual_projection = getattr(self.model, "visual_projection", None)
        out_features = getattr(visual_projection, "out_features", None)
        if out_features is not None:
            return int(out_features)
        return 512

    def _batched(self, items: list) -> Iterable[list]:
        for start in range(0, len(items), self.batch_size):
            yield items[start : start + self.batch_size]

    def _empty_embeddings(self) -> np.ndarray:
        return np.zeros((0, self.embedding_dim), dtype=np.float32)

    def _encode(
        self,
        items: list,
        *,
        input_key: str,
        with_norms: bool = False,
        processor_kwargs: dict | None = None,
    ) -> tuple[np.ndarray, np.ndarray | None]:
        if not items:
            return self._empty_embeddings(), np.zeros(
                (0,), dtype=np.float32
            ) if with_norms else None
        all_embeddings = []
        all_norms = []
        for batch_items in self._batched(items):
            inputs = self.processor(
                return_tensors="pt", **(processor_kwargs or {}), **{input_key: batch_items}
            )
            inputs = {key: value.to(self.device) for key, value in inputs.items()}
            with torch.no_grad():
                features = (
                    self.model.get_image_features(**inputs)
                    if input_key == "images"
                    else self.model.get_text_features(**inputs)
                )
                features = _ensure_feature_tensor(features)
                if with_norms:
                    all_norms.append(features.norm(dim=-1).cpu().numpy().astype(np.float32))
                features = _normalize(features)
            all_embeddings.append(features.cpu().numpy().astype(np.float32))
        embeddings = np.concatenate(all_embeddings, axis=0)
        norms = np.concatenate(all_norms, axis=0) if with_norms else None
        return embeddings, norms

    def encode_images(self, image_paths: Iterable[str]) -> EncodedBatch:
        images = [Image.open(path).convert("RGB") for path in image_paths]
        embeddings, norms = self._encode(images, input_key="images", with_norms=True)
        return EncodedBatch(
            embeddings=embeddings,
            norms=norms if norms is not None else np.zeros((0,), dtype=np.float32),
        )

    def encode_texts(self, texts: Iterable[str]) -> np.ndarray:
        items = [text.strip() for text in texts if text and text.strip()]
        embeddings, _ = self._encode(
            items,
            input_key="text",
            processor_kwargs={"padding": True, "truncation": True, "max_length": 512},
        )
        return embeddings
