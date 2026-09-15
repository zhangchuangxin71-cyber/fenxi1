from __future__ import annotations

import logging
import threading

from embed_core.bge.embedder import HuggingFaceBgeTextEmbedder
from embed_core.clip.embedding import ChineseClipEncoder
from embed_core.clip.image_encode import encode_pil_images
from embed_core.vector_utils import l2_normalize

from .config import ServiceConfig
from .errors import ModelError, ValidationError
from .user_errors import REASON_EMPTY_TEXT, REASON_MODEL_ERROR

logger = logging.getLogger(__name__)


class ModelRegistry:
    """Thread-safe model registry with lazy loading."""

    def __init__(self, config: ServiceConfig):
        self.config = config
        self._lock = threading.Lock()
        self._clip_encoder: ChineseClipEncoder | None = None
        self._bge_embedder: HuggingFaceBgeTextEmbedder | None = None
        self._clip_loading = False
        self._bge_loading = False

    def get_clip_encoder(self) -> ChineseClipEncoder:
        """Get CLIP encoder with lazy loading and thread safety."""
        if self._clip_encoder is not None:
            return self._clip_encoder

        with self._lock:
            # Double-check after acquiring lock
            if self._clip_encoder is not None:
                return self._clip_encoder

            if self._clip_loading:
                raise ModelError(
                    "CLIP model is currently being loaded by another thread",
                    details={"reason": REASON_MODEL_ERROR},
                )

            self._clip_loading = True
            try:
                logger.info(f"Loading CLIP model from {self.config.clip_model_path}")
                self._clip_encoder = ChineseClipEncoder(
                    model_path=self.config.clip_model_path,
                    device=self.config.clip_device,
                    batch_size=self.config.clip_batch_size,
                )
                logger.info(f"CLIP model loaded successfully on {self.config.clip_device}")
                return self._clip_encoder
            except Exception as exc:
                logger.error(f"Failed to load CLIP model: {exc}")
                raise ModelError(
                    f"Failed to load CLIP model: {exc}",
                    details={"reason": REASON_MODEL_ERROR},
                ) from exc
            finally:
                self._clip_loading = False

    def get_bge_embedder(self) -> HuggingFaceBgeTextEmbedder:
        """Get BGE embedder with lazy loading and thread safety."""
        if self._bge_embedder is not None:
            return self._bge_embedder

        with self._lock:
            # Double-check after acquiring lock
            if self._bge_embedder is not None:
                return self._bge_embedder

            if self._bge_loading:
                raise ModelError(
                    "BGE model is currently being loaded by another thread",
                    details={"reason": REASON_MODEL_ERROR},
                )

            self._bge_loading = True
            try:
                # Use local path if available, otherwise use model name from HuggingFace
                model_identifier = self.config.bge_model_path or self.config.bge_model_name
                logger.info(f"Loading BGE model: {model_identifier}")
                
                self._bge_embedder = HuggingFaceBgeTextEmbedder(
                    model_name=model_identifier,
                    device=self.config.bge_device,
                    batch_size=self.config.bge_batch_size,
                    local_files_only=self.config.bge_local_files_only,
                )
                logger.info(f"BGE model loaded successfully on {self.config.bge_device}")
                return self._bge_embedder
            except Exception as exc:
                logger.error(f"Failed to load BGE model: {exc}")
                raise ModelError(
                    f"Failed to load BGE model: {exc}",
                    details={"reason": REASON_MODEL_ERROR},
                ) from exc
            finally:
                self._bge_loading = False

    def is_clip_loaded(self) -> bool:
        """Check if CLIP model is loaded."""
        return self._clip_encoder is not None

    def is_bge_loaded(self) -> bool:
        """Check if BGE model is loaded."""
        return self._bge_embedder is not None


def encode_image_clip(registry: ModelRegistry, image) -> tuple[list[float], str]:
    """Encode image using CLIP model."""
    encoder = registry.get_clip_encoder()
    vector = encode_pil_images(encoder, [image])[0]
    return vector.astype("float32").tolist(), encoder.model_name


def encode_clip_text(registry: ModelRegistry, text: str) -> tuple[list[float], str]:
    """
    Encode single text using CLIP model (optimized for query).
    Direct encoding without batch processing overhead for minimal latency.
    """
    import torch
    import numpy as np
    
    encoder = registry.get_clip_encoder()
    
    # Direct single-text encoding bypassing batch processing
    text = text.strip()
    if not text:
        raise ValidationError("Empty text", details={"reason": REASON_EMPTY_TEXT})
    
    # Process single text directly
    inputs = encoder.processor(
        text=[text],
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=512,
    )
    inputs = {key: value.to(encoder.device) for key, value in inputs.items()}
    
    with torch.no_grad():
        outputs = encoder.model.get_text_features(**inputs)
        
        # Extract tensor from model output (same logic as _ensure_feature_tensor)
        if isinstance(outputs, torch.Tensor):
            features = outputs
        elif hasattr(outputs, "text_embeds") and outputs.text_embeds is not None:
            features = outputs.text_embeds
        elif hasattr(outputs, "pooler_output") and outputs.pooler_output is not None:
            features = outputs.pooler_output
        elif hasattr(outputs, "last_hidden_state") and outputs.last_hidden_state is not None:
            features = outputs.last_hidden_state[:, 0]
        else:
            raise TypeError(f"Unsupported feature output type: {type(outputs)!r}")
        
        # Normalize
        features = features / features.norm(dim=-1, keepdim=True).clamp_min(1e-12)
        vector = features.cpu().numpy().astype(np.float32)[0]
    
    return vector.tolist(), encoder.model_name


def encode_bge_text(registry: ModelRegistry, text: str) -> tuple[list[float], str]:
    """
    Encode single text using BGE model (optimized for query).
    Direct encoding without batch processing overhead for minimal latency.
    """
    import torch
    import numpy as np
    embedder = registry.get_bge_embedder()
    
    # Direct single-text encoding bypassing batch processing
    text = text.strip()
    if not text:
        raise ValidationError("Empty text", details={"reason": REASON_EMPTY_TEXT})
    
    # Process single text directly
    inputs = embedder.tokenizer(
        [text],
        padding=True,
        truncation=True,
        max_length=512,
        return_tensors="pt",
    )
    inputs = {key: value.to(embedder.device) for key, value in inputs.items()}
    
    with torch.no_grad():
        outputs = embedder.model(**inputs)
        # Use CLS token embedding (first token)
        embeddings = outputs.last_hidden_state[:, 0]
        vector = l2_normalize(embeddings.cpu().numpy().astype(np.float32)[0])
    
    return vector.tolist(), embedder.model_name
