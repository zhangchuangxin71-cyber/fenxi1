import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field


class NodeModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    node_id: Optional[Union[int, str]] = None
    text: Optional[str] = None
    page_number: Optional[int] = None
    level: int = 1
    summary: Optional[str] = None
    keywords: Optional[List[str]] = None
    nodes: List["NodeModel"] = Field(default_factory=list)


class DocStructureModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    doc_type: str
    doc_name: str
    file_path: Optional[str] = None
    created_at: Optional[str] = None
    modified_at: Optional[str] = None
    status: str = "ok"
    nodes: List[NodeModel] = Field(default_factory=list)
    error: Optional[Dict[str, Any]] = None


def _model_validate(model_cls: Any, data: Any) -> Any:
    if hasattr(model_cls, "model_validate"):
        return model_cls.model_validate(data)
    return model_cls.parse_obj(data)


def _model_dump(instance: Any) -> Dict[str, Any]:
    if hasattr(instance, "model_dump"):
        return instance.model_dump(exclude_none=True)
    return instance.dict(exclude_none=True)


def validate_output_structure(structure: Dict[str, Any]) -> Dict[str, Any]:
    validated = _model_validate(DocStructureModel, structure)
    return _model_dump(validated)


def _format_legacy_node(node: Dict[str, Any], idx: int) -> Dict[str, Any]:
    raw_id = node.get("node_id", idx)
    node_id = f"{raw_id:04d}" if isinstance(raw_id, int) else str(raw_id)

    title = node.get("title")
    if not title:
        text = (node.get("text") or "").strip()
        title = text[:30] if text else f"Node {node_id}"

    start_index = (
        node.get("start_index")
        or node.get("page_number")
        or node.get("paragraph_index")
        or node.get("line_start")
        or node.get("sheet_index")
        or node.get("slide_number")
        or idx
    )
    end_index = (
        node.get("end_index")
        or node.get("line_end")
        or node.get("page_number")
        or node.get("paragraph_index")
        or node.get("sheet_index")
        or node.get("slide_number")
        or start_index
    )

    out: Dict[str, Any] = {
        "title": title,
        "node_id": node_id,
        "start_index": (
            int(start_index)
            if isinstance(start_index, (int, float, str)) and str(start_index).isdigit()
            else start_index
        ),
        "end_index": (
            int(end_index)
            if isinstance(end_index, (int, float, str)) and str(end_index).isdigit()
            else end_index
        ),
        "summary": node.get("summary") or "",
    }

    children = node.get("nodes") or []
    formatted_children: List[Dict[str, Any]] = []
    for cidx, child in enumerate(children, 1):
        if isinstance(child, dict):
            formatted_children.append(_format_legacy_node(child, cidx))
    if formatted_children:
        out["nodes"] = formatted_children

    return out


def _parse_md_page_spans(file_path: str) -> Tuple[int, List[Tuple[int, int, int]]]:
    """
    Parse markdown page spans from explicit markers.
    Supported forms:
    - ## Page 12
    - [[PAGE 12]]
    Returns: (total_lines, [(page_num, start_line, end_line), ...]).
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return 0, []

    total_lines = len(lines)
    markers: List[Tuple[int, int]] = []
    heading_pattern = re.compile(r"^\s*##\s*Page\s+(\d+)\b", flags=re.IGNORECASE)
    marker_pattern = re.compile(r"^\s*\[\[PAGE\s+(\d+)\]\]\s*$", flags=re.IGNORECASE)
    for idx, line in enumerate(lines, 1):
        stripped = (line or "").strip()
        m = heading_pattern.match(stripped) or marker_pattern.match(stripped)
        if m:
            markers.append((int(m.group(1)), idx))

    if not markers:
        return total_lines, []

    spans: List[Tuple[int, int, int]] = []
    for i, (page_num, start_line) in enumerate(markers):
        if i + 1 < len(markers):
            end_line = markers[i + 1][1] - 1
        else:
            end_line = total_lines
        spans.append((page_num, start_line, end_line))
    return total_lines, spans


def _make_line_to_index_mapper(
    spans: List[Tuple[int, int, int]],
) -> Callable[[Optional[int]], Optional[int]]:
    def line_to_index(line_no: Optional[int]) -> Optional[int]:
        if not isinstance(line_no, int) or line_no <= 0:
            return None
        for page_num, start_line, end_line in spans:
            if start_line <= line_no <= end_line:
                return int(page_num)
        return None

    return line_to_index


def _infer_md_indices_recursive(
    nodes: List[Dict[str, Any]],
    line_to_index: Callable[[Optional[int]], Optional[int]],
    default_end_line: int,
) -> None:
    if not isinstance(nodes, list):
        return

    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue

        start_line = node.get("line_num") if isinstance(node.get("line_num"), int) else None
        next_line = None
        if i + 1 < len(nodes):
            sibling_line = nodes[i + 1].get("line_num") if isinstance(nodes[i + 1], dict) else None
            next_line = sibling_line if isinstance(sibling_line, int) else None

        end_line = default_end_line
        if isinstance(next_line, int):
            end_line = max(1, next_line - 1)

        children = node.get("nodes")
        if isinstance(children, list) and children:
            _infer_md_indices_recursive(children, line_to_index, end_line)

        if "start_index" not in node:
            mapped_start = line_to_index(start_line)
            if isinstance(mapped_start, int):
                node["start_index"] = mapped_start
            elif isinstance(start_line, int):
                node["start_index"] = start_line

        if "end_index" not in node:
            mapped_end = line_to_index(end_line)
            if isinstance(mapped_end, int):
                node["end_index"] = mapped_end
            elif isinstance(end_line, int):
                node["end_index"] = end_line


def _ensure_md_legacy_indices(
    legacy_nodes: List[Dict[str, Any]],
    structure: Dict[str, Any],
    file_path: str,
) -> None:
    if not isinstance(legacy_nodes, list) or not legacy_nodes:
        return

    has_start = any(isinstance(n, dict) and "start_index" in n for n in legacy_nodes)
    if has_start:
        return

    total_lines = structure.get("line_count")
    if not isinstance(total_lines, int) or total_lines <= 0:
        total_lines = 0

    md_path = structure.get("file_path") or structure.get("path") or file_path
    parsed_total_lines, spans = _parse_md_page_spans(str(md_path))
    if parsed_total_lines > 0:
        total_lines = parsed_total_lines

    line_to_index = _make_line_to_index_mapper(spans) if spans else (lambda _x: None)
    _infer_md_indices_recursive(legacy_nodes, line_to_index, default_end_line=max(1, total_lines))


def to_legacy_output(structure: Dict[str, Any], file_path: str) -> Dict[str, Any]:
    path_obj = Path(file_path)
    doc_id = str(structure.get("id") or path_obj.stem or uuid.uuid4())
    doc_type = str(structure.get("doc_type") or structure.get("type") or "").strip().lower()

    # markdown branch already produces pageindex-like `structure`; prefer it directly.
    existing_structure = structure.get("structure")
    if isinstance(existing_structure, list):
        legacy_nodes = [node for node in existing_structure if isinstance(node, dict)]
        if doc_type in {"md", "txt"}:
            _ensure_md_legacy_indices(legacy_nodes, structure, file_path)
    else:
        nodes = structure.get("nodes") or []
        legacy_nodes: List[Dict[str, Any]] = []
        for idx, node in enumerate(nodes, 1):
            if isinstance(node, dict):
                legacy_nodes.append(_format_legacy_node(node, idx))

    pages = structure.get("pages")
    if not isinstance(pages, list):
        pages = []

    page_count = structure.get("page_count")
    if page_count is None:
        page_count = len(pages) if pages else (structure.get("paragraph_count") or len(legacy_nodes))

    line_count = structure.get("line_count")
    if line_count is None and doc_type in {"md", "txt"}:
        line_count = structure.get("paragraph_count")

    payload: Dict[str, Any] = {
        "id": doc_id,
        "type": doc_type,
        "path": structure.get("file_path") or structure.get("path") or os.path.abspath(file_path),
        "doc_name": structure.get("doc_name") or os.path.basename(file_path),
        "doc_description": structure.get("doc_description", ""),
        "structure": legacy_nodes,
        "pages": pages,
    }
    if page_count is not None:
        payload["page_count"] = page_count
    if line_count is not None:
        payload["line_count"] = line_count
    return payload


def strip_internal_fields(obj: Any) -> Any:
    if isinstance(obj, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.startswith("_"):
                continue
            cleaned[k] = strip_internal_fields(v)
        return cleaned
    if isinstance(obj, list):
        return [strip_internal_fields(x) for x in obj]
    return obj
