# 模块说明：定义文档索引配置与 BM25 预筛组件。
# 主要职责：提供分词、构建 BM25 索引、按查询召回候选文档。
import re
from dataclasses import dataclass
from pathlib import Path

try:
    from rank_bm25 import BM25Okapi
except Exception:  # pragma: no cover - optional dependency
    BM25Okapi = None


@dataclass(frozen=True)
class IndexingOptions:
    """索引阶段使用的关键超参数集合。

    该结构体用于在索引主流程中统一传递配置，避免函数参数爆炸。
    字段覆盖了长文档分块策略、摘要开关、OCR 兜底、并发与超时等能力。
    """
    simple_index_page_threshold: int
    long_pdf_hard_threshold: int
    simple_index_chunk_pages: int
    hybrid_index_chunk_pages: int
    chunk_overlap_ratio: float
    long_pdf_mode: str
    complexity_sample_pages: int
    index_concurrency: int
    summary_enabled: bool
    table_parse_mode: str
    node_max_tokens: int
    max_tree_depth: int
    summary_concurrency: int = 0
    index_timeout_seconds: int = 0
    enable_ocr: bool = False
    ocr_min_chars: int = 20
    ocr_lang: str = "chi_sim+eng"


def tokenize_for_bm25(text: str) -> list[str]:
    """将文本切分为适合 BM25 的 token 列表。

    说明：
    - 英文/数字按连续片段切分；
    - 中文按单字切分，并额外补充相邻双字 token；
    - 双字 token 可提升中文短语召回能力（如“光明”“乳鸽”）。
    """
    if not text:
        return []
    lowered = text.lower()
    tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", lowered)
    if not tokens:
        return []

    merged = list(tokens)
    cjk_chars = [tok for tok in merged if len(tok) == 1 and "\u4e00" <= tok <= "\u9fff"]
    for i in range(len(cjk_chars) - 1):
        merged.append(cjk_chars[i] + cjk_chars[i + 1])
    return merged


class BM25Prefilter:
    """文档级 BM25 预筛器。

    作用：
    - 在进入 Agent 细粒度检索前，先从文档目录中过滤候选文档；
    - 降低后续工具调用次数，减少无关文档噪声。
    """
    def __init__(self, catalog: list[dict]):
        """基于文档目录构建 BM25 语料与索引后端。"""
        self.catalog = catalog
        self.catalog_by_id = {item["doc_id"]: item for item in catalog}
        self.doc_ids = [item["doc_id"] for item in catalog]
        corpus = []
        for item in catalog:
            joined = "\n".join(
                [
                    item.get("doc_name", ""),
                    item.get("doc_description", ""),
                    Path(item.get("path", "")).name,
                ]
            )
            corpus.append(tokenize_for_bm25(joined))
        self.tokenized_corpus = corpus
        self.backend = BM25Okapi(corpus) if BM25Okapi is not None and corpus else None

    def retrieve(
        self,
        query: str,
        top_k: int,
        allowed_doc_ids: set[str] | None = None,
    ) -> list[dict]:
        """按查询召回 Top-K 文档。

        召回逻辑：
        1) 优先使用 rank_bm25 打分；
        2) 若依赖缺失，则退化为 token 重叠计数；
        3) 若查询无 token 或无正分文档，返回受限范围内的前若干文档作为兜底。
        """
        if top_k <= 0 or not self.catalog:
            return []

        candidate_ids = set(self.doc_ids if allowed_doc_ids is None else allowed_doc_ids)
        if not candidate_ids:
            return []

        query_tokens = tokenize_for_bm25(query)
        if not query_tokens:
            fallback = [item for item in self.catalog if item["doc_id"] in candidate_ids]
            return fallback[:top_k]

        scored: list[tuple[float, dict]] = []
        if self.backend is not None:
            scores = self.backend.get_scores(query_tokens)
            for idx, score in enumerate(scores):
                doc_id = self.doc_ids[idx]
                if doc_id not in candidate_ids:
                    continue
                scored.append((float(score), self.catalog_by_id[doc_id]))
        else:
            query_set = set(query_tokens)
            for item, doc_tokens in zip(self.catalog, self.tokenized_corpus):
                if item["doc_id"] not in candidate_ids:
                    continue
                overlap = len(query_set & set(doc_tokens))
                if overlap > 0:
                    scored.append((float(overlap), item))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        selected = [item for score, item in scored if score > 0][:top_k]
        if selected:
            return selected

        fallback = [item for item in self.catalog if item["doc_id"] in candidate_ids]
        return fallback[:top_k]

