from __future__ import annotations

import os

from embed_core.milvus_store import (
    PictureMilvusStore,
    collection_name,
    hybrid_retrieval_collection,
)


DEFAULT_IMAGE_CLIP_DIM = 1024
DEFAULT_IMAGE_BGE_DIM = 1024
DEFAULT_VIDEO_CLIP_DIM = 1024
DEFAULT_VIDEO_BGE_DIM = 1024


def _create_collection(kind: str, dim: int) -> None:
    store = PictureMilvusStore(collection=collection_name(kind, None), dim=dim)
    store.close()


def main() -> None:
    image_clip_dim = int(os.environ.get("IMAGE_CLIP_DIM", str(DEFAULT_IMAGE_CLIP_DIM)))
    image_bge_dim = int(os.environ.get("IMAGE_BGE_DIM", str(DEFAULT_IMAGE_BGE_DIM)))
    video_clip_dim = int(os.environ.get("VIDEO_CLIP_DIM", str(DEFAULT_VIDEO_CLIP_DIM)))
    video_bge_dim = int(os.environ.get("VIDEO_BGE_DIM", str(DEFAULT_VIDEO_BGE_DIM)))

    _create_collection("media_image_clip", image_clip_dim)
    _create_collection("media_image_bge", image_bge_dim)
    _create_collection("media_video_clip", video_clip_dim)
    _create_collection("media_video_bge", video_bge_dim)

    store = PictureMilvusStore(
        collection=hybrid_retrieval_collection(None),
        dim=video_bge_dim,
    )
    store.close()

    print("Milvus collections are initialized.")


if __name__ == "__main__":
    main()
