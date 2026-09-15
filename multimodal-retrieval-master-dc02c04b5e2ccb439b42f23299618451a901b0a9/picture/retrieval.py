from __future__ import annotations

import time
from pathlib import Path

from .encoder import ChineseClipEncoder, encode_pil_images
from .image_io import load_image_rgb
from .image_resolve import resolve_image_file
from .metadata_store import PictureMetadataStore
from .milvus_store import PictureMilvusStore, collection_name, milvus_enabled
from .portable_paths import resolve_portable_path
from .profile_paths import default_metadata_db_path, default_output_dir
from .vector_utils import l2_normalize


class PictureRetriever:
    def __init__(
        self,
        *,
        encoder: ChineseClipEncoder,
        store: PictureMetadataStore,
        milvus_store: PictureMilvusStore,
        search_roots: list[Path] | None = None,
    ):
        self.encoder = encoder
        self.store = store
        self.search_roots = search_roots or []
        self.milvus_store = milvus_store

    @classmethod
    def load(
        cls,
        *,
        output_dir: Path,
        metadata_db_path: Path,
        model_path: str,
        device: str = "cuda",
        batch_size: int = 16,
        profile: str | None = None,
    ) -> PictureRetriever:
        if not milvus_enabled():
            raise RuntimeError(
                "Picture retrieval requires Milvus (VECTOR_STORE=milvus, default). "
                "FAISS indexes are no longer supported."
            )
        encoder = ChineseClipEncoder(model_path=model_path, device=device, batch_size=batch_size)
        store = PictureMetadataStore(metadata_db_path)
        milvus_store = PictureMilvusStore(
            collection=collection_name("picture_clip", profile),
            dim=encoder.embedding_dim,
        )
        return cls(encoder=encoder, store=store, search_roots=[], milvus_store=milvus_store)

    def search_text(
        self,
        query: str,
        *,
        top_k: int = 10,
        dedupe: bool = True,
        dedupe_method: str = "md5",
        dedupe_similarity: float = 0.99,
    ) -> tuple[list[dict], float]:
        vectors = self.encoder.encode_texts([query])
        if vectors.size == 0:
            return [], 0.0
        query_vec = l2_normalize(vectors[0])
        started = time.perf_counter()
        fetch_k = top_k * 3 if dedupe else top_k
        hits = self.milvus_store.search(query_vec, top_k=fetch_k)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        results = [self._milvus_hit_to_dict(hit) for hit in hits]
        if dedupe:
            results = self._dedupe_results(results, top_k=top_k, method=dedupe_method)
        else:
            results = results[:top_k]
        return results, elapsed_ms

    def search_image(
        self,
        source: str | Path,
        *,
        top_k: int = 10,
        dedupe: bool = True,
        dedupe_method: str = "md5",
        dedupe_similarity: float = 0.99,
    ) -> tuple[list[dict], float]:
        image = load_image_rgb(source)
        query_vec = l2_normalize(encode_pil_images(self.encoder, [image])[0])
        started = time.perf_counter()
        fetch_k = top_k * 3 if dedupe else top_k
        hits = self.milvus_store.search(query_vec, top_k=fetch_k)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        results = [self._milvus_hit_to_dict(hit) for hit in hits]
        if dedupe:
            results = self._dedupe_results(results, top_k=top_k, method=dedupe_method)
        else:
            results = results[:top_k]
        return results, elapsed_ms

    def _dedupe_results(self, results: list[dict], *, top_k: int, method: str) -> list[dict]:
        seen: set[str] = set()
        kept: list[dict] = []
        for item in results:
            image_id = str(item.get("image_id") or "")
            if method == "md5":
                row = self.store.get_image(image_id) or {}
                key = str(row.get("md5") or image_id)
            else:
                key = image_id
            if key in seen:
                continue
            seen.add(key)
            kept.append(item)
            if len(kept) >= top_k:
                break
        return kept

    def _milvus_hit_to_dict(self, hit: dict) -> dict:
        image_id = str(hit.get("image_id") or hit.get("pk") or "")
        path_text = str(hit.get("path") or image_id)
        return {
            "image_id": image_id,
            "score": float(hit.get("score") or 0.0),
            "path": path_text,
            "resolved_path": str(resolve_portable_path(path_text)),
        }

    def resolve_path(self, image_id: str, image_root: Path | None = None) -> Path | None:
        row = self.milvus_store.get(image_id)
        if not row:
            return None
        roots: list[Path] = []
        if image_root:
            roots.append(Path(image_root))
        roots.extend(self.search_roots)
        return resolve_image_file(image_id, str(row.get("path") or ""), roots)

    def index_count(self) -> int:
        return self.milvus_store.count()

    def close(self) -> None:
        self.store.close()
        self.milvus_store.close()


def build_retriever(
    *,
    profile: str | None,
    model_path: str,
    output_dir: Path | None = None,
    metadata_db: Path | None = None,
    device: str = "cuda",
    search_roots: list[Path] | None = None,
) -> PictureRetriever:
    resolved_output = output_dir or default_output_dir(profile)
    resolved_db = metadata_db or default_metadata_db_path(profile)
    retriever = PictureRetriever.load(
        output_dir=resolved_output,
        metadata_db_path=resolved_db,
        model_path=model_path,
        device=device,
        profile=profile,
    )
    if search_roots:
        retriever.search_roots = [Path(p).resolve() for p in search_roots]
    return retriever
