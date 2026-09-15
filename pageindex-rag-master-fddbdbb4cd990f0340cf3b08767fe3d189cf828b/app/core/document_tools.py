# 模块说明：文档目录、结构、分页读取和文档限制工具。
import logging
from pathlib import Path

from pydantic import ValidationError

from core.common import _is_within_directory, log_event
from models.schemas import DocumentCatalogItem
from utils.runtime import get_client_runtime

logger = logging.getLogger(__name__)


# 返回文档目录列表。
# 如果传了 allowed_doc_ids，就只返回这些 doc_id 对应的文档。
def list_documents_tool(client, allowed_doc_ids: set[str] | None = None) -> list[dict]:
    """整理当前可访问的文档目录，返回给上层做检索或展示。"""
    documents = []
    for doc_id, doc in client.documents.items():
        if allowed_doc_ids is not None and doc_id not in allowed_doc_ids:
            continue
        item = {
            "doc_id": doc_id,
            "doc_name": doc.get("doc_name", ""),
            "doc_description": doc.get("doc_description", ""),
            "type": doc.get("type", ""),
            "path": doc.get("path", ""),
            "index_mode": doc.get("index_mode", "standard"),
        }
        if doc.get("type") == "pdf":
            item["page_count"] = doc.get("page_count", 0)
        else:
            item["line_count"] = doc.get("line_count", 0)
        try:
            documents.append(DocumentCatalogItem.model_validate(item).model_dump())
        except ValidationError as exc:
            log_event("catalog_item_invalid", level=logging.WARNING, doc_id=doc_id, error=str(exc))
    documents.sort(key=lambda item: item["doc_name"])
    return documents


# 获取单个文档的基础信息。
def get_document_tool(client, doc_id: str) -> str:
    """按 doc_id 返回单个文档的元信息字符串。"""
    return client.get_document(doc_id)


# 获取文档结构树。
# 优先从内存中的文档对象读取；如果拿不到，再调用 client 接口。
def get_document_structure_tool(client, doc_id: str):
    """返回文档结构树（优先内存，其次后端接口）。

    这样做可以减少重复序列化/反序列化开销，并在接口异常时提供兜底。
    """
    try:
        if hasattr(client, "_ensure_doc_loaded"):
            client._ensure_doc_loaded(doc_id)
        doc = client.documents.get(doc_id, {}) if hasattr(client, "documents") else {}
        structure = doc.get("structure")
        if isinstance(structure, list):
            return structure
    except Exception:
        pass
    try:
        return json.loads(client.get_document_structure(doc_id))
    except Exception as exc:
        log_event("document_structure_parse_failed", level=logging.ERROR, doc_id=doc_id, error=str(exc))
        return []


# 把页码表达式解析成页码列表。
# 例如：
# - "12" -> [12]
# - "3-5" -> [3, 4, 5]
# - "1,3-5" -> [1, 3, 4, 5]
def parse_pages_expression(pages: str) -> list[int]:
    """解析页码表达式并返回去重、排序后的页码列表。

    支持格式：
    - 单页：`12`
    - 区间：`3-5`
    - 混合：`1,3-5,8`
    """
    normalized = (pages or "").strip()
    if not normalized:
        return []
    result: list[int] = []
    for part in normalized.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start, end = token.split("-", 1)
            start_i = int(start.strip())
            end_i = int(end.strip())
            if start_i > end_i:
                start_i, end_i = end_i, start_i
            result.extend(range(start_i, end_i + 1))
        else:
            result.append(int(token))
    return sorted(set(result))


# 读取指定页的正文内容。
# 为了避免重复读取，同一文档同一页范围会走缓存；
# 如果文档内容变了，缓存键也会跟着变化。
def get_page_content_tool(client, doc_id: str, pages: str):
    """按页码范围读取正文内容，并带文档版本感知缓存。

    缓存键包含文档版本字段（如 content_hash），
    可避免文档更新后命中旧页面内容。
    """
    runtime = get_client_runtime(client)
    normalized_pages = (pages or "").strip()
    doc_meta = client.documents.get(doc_id, {}) if hasattr(client, "documents") else {}
    doc_version = str(
        doc_meta.get("content_hash")
        or doc_meta.get("file_head_md5")
        or doc_meta.get("file_md5")
        or ""
    )
    cache_key = (str(id(client)), doc_id, normalized_pages, doc_version)
    cached = runtime.page_content_cache.get(cache_key)
    if cached is not None:
        logger.debug(
            "page_cache_hit doc_id=%s pages=%s hits=%s misses=%s",
            doc_id,
            normalized_pages,
            runtime.page_content_cache.hits,
            runtime.page_content_cache.misses,
        )
        return cached
    try:
        if hasattr(client, "_ensure_doc_loaded"):
            client._ensure_doc_loaded(doc_id)
        doc = client.documents.get(doc_id, {}) if hasattr(client, "documents") else {}
        page_entries = doc.get("pages")
        if isinstance(page_entries, list) and page_entries:
            page_nums = parse_pages_expression(normalized_pages)
            page_map = {
                int(item.get("page")): str(item.get("content", "") or "")
                for item in page_entries
                if isinstance(item, dict) and item.get("page") is not None
            }
            payload_native = [{"page": p, "content": page_map[p]} for p in page_nums if p in page_map]
            runtime.page_content_cache.set(cache_key, payload_native)
            return payload_native

        payload = json.loads(client.get_page_content(doc_id, normalized_pages))
        if isinstance(payload, list):
            runtime.page_content_cache.set(cache_key, payload)
            logger.debug(
                "page_cache_store doc_id=%s pages=%s size=%s",
                doc_id,
                normalized_pages,
                len(payload),
            )
            return payload
        return []
    except Exception as exc:
        log_event(
            "page_content_parse_failed",
            level=logging.ERROR,
            doc_id=doc_id,
            pages=normalized_pages,
            error=str(exc),
        )
        return []


# 解析 restrict_docs 参数。
# 用户既可以传 doc_id，也可以传文件名；这里统一解析成 doc_id 集合。
def resolve_restricted_doc_ids(client, raw_restrict_docs: str | None) -> set[str] | None:
    """把 restrict_docs 文本解析成 doc_id 集合。

    允许输入 doc_id、doc_name、文件名，或展示给用户的去扩展名名称，
    最终统一为 doc_id 供检索链路使用。
    """
    if not raw_restrict_docs:
        return None

    tokens = [part.strip() for part in raw_restrict_docs.split(",") if part.strip()]
    if not tokens:
        return None

    catalog = list_documents_tool(client)
    by_id = {item["doc_id"]: item["doc_id"] for item in catalog}
    by_name_lower: dict[str, list[str]] = {}
    by_path_name_lower: dict[str, list[str]] = {}
    stem_candidates: dict[str, list[str]] = {}
    for item in catalog:
        doc_id = item["doc_id"]
        doc_name = str(item.get("doc_name", "") or "").strip()
        file_name = Path(str(item.get("path", "") or "")).name.strip()
        if doc_name:
            by_name_lower.setdefault(doc_name.lower(), [])
            if doc_id not in by_name_lower[doc_name.lower()]:
                by_name_lower[doc_name.lower()].append(doc_id)
        if file_name:
            by_path_name_lower.setdefault(file_name.lower(), [])
            if doc_id not in by_path_name_lower[file_name.lower()]:
                by_path_name_lower[file_name.lower()].append(doc_id)
        for raw_name in (doc_name, file_name):
            if not raw_name:
                continue
            stem = Path(raw_name).stem.strip().lower()
            if not stem:
                continue
            stem_candidates.setdefault(stem, [])
            if doc_id not in stem_candidates[stem]:
                stem_candidates[stem].append(doc_id)

    resolved = []
    unresolved = []
    for token in tokens:
        matched_doc_id = by_id.get(token)
        matched_doc_ids: list[str] = [matched_doc_id] if matched_doc_id is not None else []
        if not matched_doc_ids:
            token_lower = token.lower()
            matched_doc_ids = (
                by_name_lower.get(token_lower)
                or by_path_name_lower.get(token_lower)
                or stem_candidates.get(token_lower)
                or []
            )
        if not matched_doc_ids:
            unresolved.append(token)
            continue
        resolved.extend(matched_doc_ids)

    if unresolved:
        raise ValueError(
            "Unknown restricted docs: "
            f"{unresolved}. Use doc_id, exact file name, or the displayed document name shown in the catalog."
        )
    if not resolved:
        raise ValueError("No valid restricted documents were resolved.")

    return set(resolved)


# 清理 PostgreSQL 中越界的缓存文档。
# 只保留 data_dir 范围内的缓存，避免误用其他目录留下的旧索引。
def prune_untrusted_cached_documents(client, data_dir: Path) -> int:
    """删除不在 data_dir 范围内的缓存文档，并返回删除数量。

    目的：防止历史缓存文档越权参与当前问答，降低数据串扰风险。
    """
    removed_ids: list[str] = []
    store = getattr(client, "_store", None)
    for doc_id, doc in list(client.documents.items()):
        raw_path = str(doc.get("path", "") or "")
        if not raw_path:
            continue
        try:
            doc_path = Path(raw_path).resolve()
        except Exception:
            client.documents.pop(doc_id, None)
            removed_ids.append(doc_id)
            continue
        if not _is_within_directory(doc_path, data_dir):
            client.documents.pop(doc_id, None)
            removed_ids.append(doc_id)

    if removed_ids and store is not None:
        for doc_id in removed_ids:
            try:
                store.delete_doc(doc_id)
            except Exception as exc:
                log_event("storage_doc_delete_failed", level=logging.WARNING, doc_id=doc_id, error=str(exc))
    return len(removed_ids)

