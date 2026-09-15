"""Re-export from embed_core for backward compatibility."""

from embed_core.clip.embedding import ChineseClipEncoder, EncodedBatch, _ensure_feature_tensor, _normalize

__all__ = ["ChineseClipEncoder", "EncodedBatch", "_ensure_feature_tensor", "_normalize"]
