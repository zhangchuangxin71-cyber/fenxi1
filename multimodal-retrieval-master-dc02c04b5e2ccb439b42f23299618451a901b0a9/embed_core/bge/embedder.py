from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

try:
    from tqdm.auto import tqdm
except ImportError:
    tqdm = None


DEFAULT_BGE_MODEL_NAME = "BAAI/bge-large-zh-v1.5"
DEFAULT_BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
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


def _hf_dependencies():
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError(
            "BGE dense retrieval requires torch and transformers in the active Python environment."
        ) from exc
    return torch, AutoModel, AutoTokenizer


class HuggingFaceBgeTextEmbedder:
    def __init__(
        self,
        *,
        model_name: str = DEFAULT_BGE_MODEL_NAME,
        device: str = "cuda",
        batch_size: int = 16,
        max_length: int = 512,
        query_instruction: str = DEFAULT_BGE_QUERY_INSTRUCTION,
        local_files_only: bool = False,
        use_fp16: bool | None = None,
    ):
        torch, AutoModel, AutoTokenizer = _hf_dependencies()

        resolved_device = str(device or "cuda").strip() or "cuda"
        if resolved_device.startswith("cuda") and not torch.cuda.is_available():
            resolved_device = "cpu"
        self._torch = torch
        self.device = torch.device(resolved_device)
        self.model_name = model_name
        self.batch_size = max(1, int(batch_size))
        self.max_length = max(8, int(max_length))
        self.query_instruction = str(query_instruction or "")
        self.local_files_only = bool(local_files_only)
        self.use_fp16 = bool(use_fp16) if use_fp16 is not None else self.device.type == "cuda"

        self.tokenizer = AutoTokenizer.from_pretrained(
            model_name,
            local_files_only=self.local_files_only,
        )
        self.model = AutoModel.from_pretrained(
            model_name,
            local_files_only=self.local_files_only,
        )
        if self.use_fp16 and self.device.type == "cuda":
            self.model = self.model.half()
        self.model = self.model.to(self.device)
        self.model.eval()

        hidden_size = getattr(self.model.config, "hidden_size", None)
        if hidden_size is None:
            raise RuntimeError(f"Unable to determine hidden_size for BGE model: {model_name}")
        self.vector_dim = int(hidden_size)
        self.backend = "huggingface-bge"

    @property
    def model_revision(self) -> str:
        commit_hash = getattr(self.model.config, "_commit_hash", None)
        if commit_hash:
            return str(commit_hash)
        return "unknown"

    def _batched(self, texts: list[str]) -> Iterable[list[str]]:
        for start in range(0, len(texts), self.batch_size):
            yield texts[start : start + self.batch_size]

    def _empty_embeddings(self) -> np.ndarray:
        return np.zeros((0, self.vector_dim), dtype=np.float32)

    def _prepare_texts(self, texts: list[str], *, add_query_instruction: bool) -> list[str]:
        prepared: list[str] = []
        for raw_text in texts:
            text = str(raw_text or "").strip()
            if add_query_instruction and text and self.query_instruction:
                prepared.append(f"{self.query_instruction}{text}")
            else:
                prepared.append(text)
        return prepared

    def _encode(
        self,
        texts: list[str],
        *,
        add_query_instruction: bool,
        progress_desc: str | None = None,
    ) -> np.ndarray:
        if not texts:
            return self._empty_embeddings()

        prepared = self._prepare_texts(texts, add_query_instruction=add_query_instruction)
        all_embeddings: list[np.ndarray] = []
        batches: Iterable[list[str]] = self._batched(prepared)
        if tqdm is not None and progress_desc:
            total_batches = (len(prepared) + self.batch_size - 1) // self.batch_size
            batches = tqdm(
                batches,
                total=total_batches,
                desc=progress_desc,
                unit="batch",
                dynamic_ncols=True,
            )
        for batch_texts in batches:
            encoded = self.tokenizer(
                batch_texts,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            )
            encoded = {key: value.to(self.device) for key, value in encoded.items()}
            with self._torch.no_grad():
                outputs = self.model(**encoded)
                sentence_embeddings = outputs.last_hidden_state[:, 0]
                sentence_embeddings = self._torch.nn.functional.normalize(
                    sentence_embeddings,
                    p=2,
                    dim=1,
                )
            all_embeddings.append(sentence_embeddings.cpu().numpy().astype(np.float32, copy=False))
        return (
            np.concatenate(all_embeddings, axis=0) if all_embeddings else self._empty_embeddings()
        )

    def encode(self, texts: list[str]) -> np.ndarray:
        return self.encode_passages(texts)

    def encode_passages(self, texts: list[str], *, progress_desc: str | None = None) -> np.ndarray:
        return self._encode(texts, add_query_instruction=False, progress_desc=progress_desc)

    def encode_queries(self, texts: list[str], *, progress_desc: str | None = None) -> np.ndarray:
        return self._encode(texts, add_query_instruction=True, progress_desc=progress_desc)

    def save(self, index_dir: Path) -> None:
        index_dir.mkdir(parents=True, exist_ok=True)
        manifest = EmbedderManifest(
            backend=self.backend,
            model_name=self.model_name,
            vector_dim=self.vector_dim,
            max_length=self.max_length,
            pooling="cls",
            normalize=True,
            query_instruction=self.query_instruction,
        )
        (index_dir / MANIFEST_FILENAME).write_text(
            json.dumps(manifest.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(
        cls,
        index_dir: Path,
        *,
        device: str = "cuda",
        batch_size: int = 16,
        local_files_only: bool = False,
        use_fp16: bool | None = None,
    ) -> HuggingFaceBgeTextEmbedder:
        manifest = _read_manifest(index_dir)
        if manifest.get("backend") != "huggingface-bge":
            raise RuntimeError(
                f"Index {index_dir} does not contain a huggingface-bge embedder manifest."
            )
        return cls(
            model_name=str(manifest.get("model_name") or DEFAULT_BGE_MODEL_NAME),
            device=device,
            batch_size=batch_size,
            max_length=int(manifest.get("max_length") or 512),
            query_instruction=str(
                manifest.get("query_instruction") or DEFAULT_BGE_QUERY_INSTRUCTION
            ),
            local_files_only=local_files_only,
            use_fp16=use_fp16,
        )
