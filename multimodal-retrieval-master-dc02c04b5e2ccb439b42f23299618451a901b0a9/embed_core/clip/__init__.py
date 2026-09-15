from .embedding import ChineseClipEncoder, EncodedBatch
from .frame_selection import select_diverse_frame_indices
from .image_encode import encode_pil_images
from .segment_embedding import softmax_attention_pooling
from .segmenter import segment_video
from .video_representation import (
    SegmentRepresentation,
    compute_frame_diff_motion_score,
    compute_global_genericness,
    compute_pairwise_segment_similarity,
    compute_segment_importance,
    select_top_representative_segments,
)

__all__ = [
    "ChineseClipEncoder",
    "EncodedBatch",
    "encode_pil_images",
    "select_diverse_frame_indices",
    "segment_video",
    "softmax_attention_pooling",
    "SegmentRepresentation",
    "compute_frame_diff_motion_score",
    "compute_segment_importance",
    "compute_global_genericness",
    "compute_pairwise_segment_similarity",
    "select_top_representative_segments",
]
