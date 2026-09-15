from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import Normalizer

try:
    from tqdm.auto import tqdm
except ImportError:
    tqdm = None

from embed_core.bge.embedder import HuggingFaceBgeTextEmbedder

DEFAULT_BGE_MODEL_NAME = "BAAI/bge-large-zh-v1.5"
DEFAULT_BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
LOCAL_SVD_MODEL_FILENAME = "local_svd_embedder.joblib"
MANIFEST_FILENAME = "embedder_manifest.json"


@dataclass
class EmbedderManifest:
    backend: str
    model_name: str
    vector_dim: int
    corpus_size: int | None = None
    max_features: int | None = None
    ngram_min: int | None = None
    ngram_max: int | None = None
    max_length: int | None = None
    pooling: str | None = None
    normalize: bool | None = None
    query_instruction: str | None = None


def _read_manifest(index_dir: Path) -> dict:
    manifest_path = index_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


class LocalSvdTextEmbedder:
    def __init__(
        self,
        *,
        vectorizer: TfidfVectorizer,
        svd: TruncatedSVD | None,
        normalizer: Normalizer,
        model_name: str,
        vector_dim: int,
        max_features: int,
        ngram_range: tuple[int, int],
        corpus_size: int,
    ):
        self.vectorizer = vectorizer
        self.svd = svd
        self.normalizer = normalizer
        self.model_name = model_name
        self.vector_dim = vector_dim
        self.max_features = max_features
        self.ngram_range = ngram_range
        self.corpus_size = corpus_size

    @classmethod
    def fit(
        cls,
        texts: list[str],
        *,
        embedding_dim: int = 256,
        max_features: int = 20000,
        model_name: str = "local-svd-char-ngram",
        ngram_range: tuple[int, int] = (2, 4),
    ) -> tuple[LocalSvdTextEmbedder, np.ndarray]:
        if not texts:
            raise ValueError("Cannot fit dense embedder on an empty corpus.")

        vectorizer = TfidfVectorizer(
            analyzer="char",
            ngram_range=ngram_range,
            max_features=max_features,
            sublinear_tf=True,
            lowercase=False,
        )
        matrix = vectorizer.fit_transform(texts)
        if matrix.shape[1] == 0:
            raise ValueError("Dense embedder failed to build a non-empty TF-IDF vocabulary.")

        normalizer = Normalizer(copy=False)
        svd: TruncatedSVD | None = None

        max_components = min(max(2, embedding_dim), matrix.shape[0] - 1, matrix.shape[1] - 1)
        if max_components >= 2:
            svd = TruncatedSVD(n_components=max_components, random_state=42)
            dense_vectors = svd.fit_transform(matrix)
        else:
            dense_vectors = matrix.toarray()

        dense_vectors = normalizer.fit_transform(dense_vectors)
        dense_vectors = np.asarray(dense_vectors, dtype=np.float32)

        embedder = cls(
            vectorizer=vectorizer,
            svd=svd,
            normalizer=normalizer,
            model_name=model_name,
            vector_dim=int(dense_vectors.shape[1]),
            max_features=max_features,
            ngram_range=ngram_range,
            corpus_size=len(texts),
        )
        return embedder, dense_vectors

    def encode(self, texts: list[str]) -> np.ndarray:
        matrix = self.vectorizer.transform(texts)
        if self.svd is not None:
            dense_vectors = self.svd.transform(matrix)
        else:
            dense_vectors = matrix.toarray()
        dense_vectors = self.normalizer.transform(dense_vectors)
        return np.asarray(dense_vectors, dtype=np.float32)

    def save(self, index_dir: Path) -> None:
        index_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "vectorizer": self.vectorizer,
            "svd": self.svd,
            "normalizer": self.normalizer,
            "model_name": self.model_name,
            "vector_dim": self.vector_dim,
            "max_features": self.max_features,
            "ngram_range": self.ngram_range,
            "corpus_size": self.corpus_size,
        }
        joblib.dump(payload, index_dir / LOCAL_SVD_MODEL_FILENAME)
        manifest = EmbedderManifest(
            backend="local-svd",
            model_name=self.model_name,
            vector_dim=self.vector_dim,
            corpus_size=self.corpus_size,
            max_features=self.max_features,
            ngram_min=self.ngram_range[0],
            ngram_max=self.ngram_range[1],
        )
        (index_dir / MANIFEST_FILENAME).write_text(
            json.dumps(manifest.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, index_dir: Path) -> LocalSvdTextEmbedder:
        payload = joblib.load(index_dir / LOCAL_SVD_MODEL_FILENAME)
        return cls(
            vectorizer=payload["vectorizer"],
            svd=payload["svd"],
            normalizer=payload["normalizer"],
            model_name=payload["model_name"],
            vector_dim=int(payload["vector_dim"]),
            max_features=int(payload["max_features"]),
            ngram_range=tuple(payload["ngram_range"]),
            corpus_size=int(payload["corpus_size"]),
        )

    def encode_passages(self, texts: list[str]) -> np.ndarray:
        return self.encode(texts)

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        return self.encode(texts)


def load_text_embedder(
    index_dir: Path,
    *,
    device: str = "cuda",
    batch_size: int = 16,
    local_files_only: bool = False,
    use_fp16: bool | None = None,
):
    manifest = _read_manifest(index_dir)
    backend = str(manifest.get("backend") or "").strip().lower()
    if not backend and (index_dir / LOCAL_SVD_MODEL_FILENAME).exists():
        backend = "local-svd"

    if backend == "huggingface-bge":
        return HuggingFaceBgeTextEmbedder.load(
            index_dir,
            device=device,
            batch_size=batch_size,
            local_files_only=local_files_only,
            use_fp16=use_fp16,
        )
    if backend == "local-svd":
        return LocalSvdTextEmbedder.load(index_dir)

    raise RuntimeError(
        f"Unsupported dense embedder backend in {index_dir}: {backend or 'missing manifest'}"
    )
