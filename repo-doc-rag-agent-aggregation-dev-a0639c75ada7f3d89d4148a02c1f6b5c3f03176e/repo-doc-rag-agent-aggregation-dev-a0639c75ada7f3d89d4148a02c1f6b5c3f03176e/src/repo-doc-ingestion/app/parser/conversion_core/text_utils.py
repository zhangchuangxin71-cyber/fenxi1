import re
from typing import Any, Dict, List, Optional, Tuple

from .tree_utils import TreeUtil


def clean_text(text: Any) -> str:
    if text is None:
        return ""
    return str(text).replace("\x00", "").strip()


def detect_text_encoding(file_path: str) -> str:
    sample_size = 65536
    with open(file_path, "rb") as f:
        raw = f.read(sample_size)

    if not raw:
        return "utf-8"

    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw).best()
        if best and best.encoding:
            return best.encoding
    except Exception:
        pass

    try:
        import chardet

        detected = chardet.detect(raw)
        enc = detected.get("encoding")
        if enc:
            return enc
    except Exception:
        pass

    candidates = ["utf-8", "utf-8-sig", "utf-16", "gb18030", "gbk", "cp1252", "latin-1"]
    for enc in candidates:
        try:
            raw.decode(enc)
            return enc
        except Exception:
            continue
    return "utf-8"


def detect_docx_heading_level(para: Any, text: str) -> Optional[int]:
    style_name = ""
    try:
        style_name = str(getattr(getattr(para, "style", None), "name", "") or "")
    except Exception:
        style_name = ""

    m = re.search(r"heading\s*(\d+)", style_name, flags=re.IGNORECASE)
    if m:
        return max(1, int(m.group(1)))

    patterns: List[Tuple[str, int]] = [
        (r"^\s*chapter\s*\d+", 1),
        (r"^\s*第[一二三四五六七八九十百千万0-9]+章", 1),
        (r"^\s*[（(]?[一二三四五六七八九十]+[)）]\s*", 2),
        (r"^\s*\d+[.、．]\s*", 3),
        (r"^\s*\d+\.\d+\s*", 3),
        (r"^\s*\d+\.\d+\.\d+\s*", 4),
    ]
    for pat, level in patterns:
        if re.match(pat, text, flags=re.IGNORECASE):
            return level
    return None


def looks_like_toc_entry(text: str) -> bool:
    t = clean_text(text)
    if not t:
        return False
    low = t.lower()
    if low in {"目录", "contents"}:
        return True
    # Common TOC leader patterns: dots/spaces/tabs ending with page numbers.
    # Covers cases like "... 17", " . 17", "．．． 17", "… 17".
    if re.search(r"(?:[.\u2026·•。．\s]{3,}|\t+)\s*\d{1,4}\s*$", t):
        return True
    if re.search(r"\.{2,}\s*\d{1,4}$", t):
        return True
    if re.search(r"\t+\s*\d{1,4}$", t):
        return True
    if re.search(r"[·•\s]{2,}\d{1,4}$", t) and len(t) <= 80:
        return True
    # TOC section-style lines with trailing page numbers.
    # Example: "（二）虹桥-华强片区 ... 17"
    if re.search(
        r"^\s*(?:chapter\s*\d+|第[一二三四五六七八九十百千万0-9]+[章节篇]|[（(]?[一二三四五六七八九十0-9]+[)）]|\d+[、.．]).*\d{1,4}\s*$",
        t,
        flags=re.IGNORECASE,
    ):
        if re.search(r"(?:[.\u2026·•。．\s]{2,}|\t+)\s*\d{1,4}\s*$", t):
            return True
    if re.search(
        r"^\s*(chapter\s*\d+|第[一二三四五六七八九十百千万0-9]+[章节篇]|[（(]?[一二三四五六七八九十0-9]+[)）]|\d+[、.．]).{0,64}\d{1,4}$",
        t,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def normalize_node_title(text: str) -> str:
    t = clean_text(text)
    if not t:
        return ""
    t = re.sub(r"[\t ]+\d{1,4}$", "", t)
    t = re.sub(r"\.{2,}\s*\d{1,4}$", "", t)
    t = re.sub(r"\s+", "", t)
    return t


def normalize_title_for_page_match(text: str) -> str:
    t = normalize_node_title(text).lower()
    t = re.sub(r"^[第chapterpart卷篇章节\\divx一二三四五六七八九十百千万0-9.\-_:：、（）()\s]+", "", t)
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", t)
    return t


def detect_pdf_heading_level(line: str) -> Optional[int]:
    text = line.strip()
    if not text:
        return None
    if looks_like_toc_entry(text):
        return None
    if re.match(r"^\s*第[0-9一二三四五六七八九十百千万]+卷第[0-9一二三四五六七八九十百千万]+期\s*$", text):
        return None
    if re.match(r"^\s*[0-9]{1,2}\s+", text):
        if re.match(r"^\s*[0-9]{1,2}\s+[a-z]", text):
            return None
        if re.search(r"[。；;，,]$", text):
            return None

    patterns: List[Tuple[str, int]] = [
        # Chinese top-level chapter markers.
        (r"^\s*第[一二三四五六七八九十百千万0-9]+[章节篇卷部]\s*[:：\-—.．、]?\s*\S+", 1),
        # English chapter markers, tolerant of no-space forms: Chapter1 / PartII.
        (r"^\s*(?:chapter|part)\s*[ivx0-9一二三四五六七八九十百千万]+\s*[:：\-—.．、]?\s*\S+", 1),
        # Chinese section markers: （一）... ; avoid treating （1） list items as headings.
        (r"^\s*[（(]\s*[一二三四五六七八九十]+\s*[）)]\s*\S+", 2),
        # Academic/article top-level markers: 0 引言 / 1 System Design.
        (r"^\s*[0-9]{1,2}\s+(?:[\u4e00-\u9fff]|(?-i:[A-Z]))\S*", 1),
        # Arabic section markers: 1、... / 1.... / 1．...
        (r"^\s*[0-9]{1,2}(?:[、．]|[.](?!\d))\s*\S+", 3),
        # Decimal heading markers: 1.1 ... / 1.1.1 ...
        (r"^\s*[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,2}(?![%\d])\s*\S+", 4),
        (r"^\s*[0-9]{1,2}\.[0-9]{1,2}(?![%\d])\s*\S+", 3),
    ]
    for pat, level in patterns:
        if re.match(pat, text, flags=re.IGNORECASE):
            return level
    return None


def compact_docx_tree(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compact DOCX tree by removing TOC-like leaves and deduping sibling titles.

    Input: list of root nodes with optional nested `nodes`.
    Output: compacted list preserving relative order for first-seen keys.
    Limit: in-place mutation of node `nodes` fields.
    """
    def _node_richness(node: Dict[str, Any]) -> int:
        text_len = len(clean_text(node.get("text")))
        child_count = len(node.get("nodes") or []) if isinstance(node.get("nodes"), list) else 0
        return child_count * 1000 + text_len

    def _compact_level(arr: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        kept: List[Dict[str, Any]] = []
        for node in arr:
            if not isinstance(node, dict):
                continue
            title = clean_text(node.get("title"))
            text = clean_text(node.get("text"))
            is_leaf = not TreeUtil.children(node)
            if is_leaf and looks_like_toc_entry(title or text):
                continue
            kept.append(node)

        deduped: Dict[str, Dict[str, Any]] = {}
        ordered_keys: List[str] = []
        for node in kept:
            key = normalize_node_title(clean_text(node.get("title")) or clean_text(node.get("text")))
            if not key:
                key = f"__node_{len(ordered_keys)}"
            if key not in deduped:
                deduped[key] = node
                ordered_keys.append(key)
                continue
            existing = deduped[key]
            if _node_richness(node) > _node_richness(existing):
                deduped[key] = node
        return [deduped[k] for k in ordered_keys if k in deduped]

    for node, _, _ in TreeUtil.iter_postorder(nodes):
        node["nodes"] = _compact_level(TreeUtil.children(node))
    return _compact_level(nodes)


def cap_pdf_tree_depth(nodes: List[Dict[str, Any]], max_depth: int) -> List[Dict[str, Any]]:
    """Cap PDF tree depth and flatten deeper descendants at target depth.

    Input: root nodes and max depth.
    Output: new root list with node levels normalized to <= max_depth.
    Limit: mutates node `nodes`/`level` in place.
    """
    if max_depth <= 0:
        return nodes

    out_roots: List[Dict[str, Any]] = []
    stack: List[Tuple[Dict[str, Any], int, List[Dict[str, Any]]]] = []
    for node in reversed(nodes):
        if isinstance(node, dict):
            stack.append((node, 1, out_roots))

    while stack:
        node, depth, out_list = stack.pop()
        children = TreeUtil.children(node)
        node["level"] = min(max_depth, max(1, depth))
        node["nodes"] = []
        out_list.append(node)

        if not children:
            continue

        if depth >= max_depth:
            for child in TreeUtil.flatten_subtree_preorder(children):
                child["level"] = max_depth
                child["nodes"] = []
                out_list.append(child)
            continue

        new_children: List[Dict[str, Any]] = []
        node["nodes"] = new_children
        for child in reversed(children):
            stack.append((child, depth + 1, new_children))

    return out_roots


def verify_docx_title_page_alignment(
    nodes: List[Dict[str, Any]],
    pages: List[Dict[str, Any]],
    paragraphs_per_page: int,
) -> Tuple[float, int, int, List[Dict[str, Any]]]:
    """Check whether node titles are found on mapped pages.

    Returns: (accuracy, passed_count, checked_count, mismatch_details).
    """
    page_text_map: Dict[int, str] = {}
    for page in pages:
        page_num = page.get("page")
        if isinstance(page_num, int):
            page_text_map[page_num] = normalize_node_title(clean_text(page.get("content")))

    checked = 0
    passed = 0
    failures: List[Dict[str, Any]] = []

    for node, _, _ in TreeUtil.iter_preorder(nodes):
        title = clean_text(node.get("title"))
        anchor = node.get("start_index")
        if not isinstance(anchor, int):
            anchor = node.get("paragraph_index")

        if not title or not isinstance(anchor, int):
            continue

        page_num = max(1, ((anchor - 1) // max(1, paragraphs_per_page)) + 1)
        page_text = page_text_map.get(page_num, "")

        checked += 1
        normalized_title = normalize_node_title(title)
        if normalized_title and page_text and normalized_title in page_text:
            passed += 1
        else:
            failures.append(
                {
                    "node_id": node.get("node_id"),
                    "title": title[:80],
                    "anchor_paragraph": anchor,
                    "mapped_page": page_num,
                    "reason": "title_not_found_in_mapped_page",
                }
            )

    accuracy = (passed / checked) if checked > 0 else 1.0
    return accuracy, passed, checked, failures
