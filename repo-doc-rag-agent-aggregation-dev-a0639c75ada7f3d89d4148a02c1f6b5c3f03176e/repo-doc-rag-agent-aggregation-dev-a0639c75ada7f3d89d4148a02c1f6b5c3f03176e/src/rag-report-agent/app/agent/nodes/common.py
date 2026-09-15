import json
import re
from datetime import datetime
from typing import Any

from app.llm.registry import get_provider
from app.rag.types import ProvidedChunkInput

USED_RE = re.compile(r"<USED_CHUNKS>\s*(\[.*?\])\s*</USED_CHUNKS>", re.DOTALL)
JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
USED_MARKER_PREFIX = "<USED_CHUNKS>"
CONTENT_CHUNK_TARGET_CHARS = 260
CONTENT_CHUNK_MIN_CHARS = 80
THINKING_CHUNK_TARGET_CHARS = 90


def get_emitter(config: dict):
    return (config or {}).get("configurable", {}).get("emitter")


async def emit_step(config: dict, step: str, message: str):
    emitter = get_emitter(config)
    if emitter:
        await emitter.emit_step(step, message)


async def emit_intent(config: dict, intent: str, confidence: float):
    emitter = get_emitter(config)
    if emitter:
        await emitter.emit_intent(intent, confidence)


async def emit_intents(config: dict, intents: list[dict]):
    emitter = get_emitter(config)
    if emitter:
        if hasattr(emitter, "emit_intents"):
            await emitter.emit_intents(intents)
        elif intents:
            await emitter.emit_intent(intents[0]["intent_type"], intents[0].get("confidence"))


def thinking_line(message: str) -> str:
    return f"{message}\n"


async def emit_thinking(config: dict, message: str):
    emitter = get_emitter(config)
    if emitter:
        await emitter.emit_thinking_delta(thinking_line(message))


def parse_json_object(text: str, default: dict | None = None) -> dict:
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text or "", flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
    return default or {}


def history_brief(history: list[dict], limit: int = 3) -> str:
    rows = []
    for item in history[-limit:]:
        rows.append(f"{item.get('role', '')}: {item.get('content', '')}")
    return "\n".join(rows) or "无"


def format_chunks(chunks: list[ProvidedChunkInput]) -> str:
    if not chunks:
        return "无"
    parts = []
    for chunk in chunks:
        parts.append(
            f"[chunk_id={chunk.chunk_id}, 来源=《{chunk.document_name}》, 路径={chunk.path}]\n"
            f"{chunk.content}"
        )
    return "\n\n".join(parts)


def format_query_groups(groups: list[dict]) -> str:
    if not groups:
        return "无"
    rendered = []
    for group in groups:
        chunks = group.get("chunks") or []
        retrieval_note = "检索状态：无需检索，按助手能力/对话上下文直接回答。" if group.get("skipped_retrieval") else ""
        rendered.append(
            "\n".join(
                [line for line in [
                    f"子问题 {group.get('id', '')}：{group.get('question', '')}".strip(),
                    f"检索改写：{group.get('retrieval_query', '')}".strip(),
                    f"回答重点：{group.get('answer_focus', '')}".strip(),
                    retrieval_note,
                    "参考资料：",
                    format_chunks(chunks),
                ] if line]
            )
        )
    return "\n\n".join(rendered)


REPORT_OUTLINE_META_KEYWORDS = (
    "报告大纲",
    "Markdown格式",
    "Markdown 格式",
    "修改后的大纲",
    "结构化JSON",
    "结构化 JSON",
    "执行大纲",
    "本次报告调整",
)


def _is_meta_outline_title(title: str) -> bool:
    normalized = (title or "").replace(" ", "")
    normalized = re.sub(r"^(第?[一二三四五六七八九十\d]+部分[：:、\-\s]*)", "", normalized)
    return any(keyword.replace(" ", "") in normalized for keyword in REPORT_OUTLINE_META_KEYWORDS)


def clean_outline_title(title: str) -> str:
    clean = str(title or "").strip()
    if not clean:
        return "报告"
    clean = re.sub(r"^[#\s]+", "", clean).strip()
    clean = re.sub(r"^(报告)?(大纲|提纲|目录|章节结构)[：:、\-\s]*", "", clean).strip()
    clean = re.sub(r"[：:、\-\s]*(大纲|提纲|目录|章节结构)$", "", clean).strip()
    return clean or "报告"


def clean_outline_item_title(title: str, index: Any = None) -> str:
    clean = str(title or "").strip()
    clean_index = str(index or "").strip()
    if clean_index:
        escaped = re.escape(clean_index)
        clean = re.sub(rf"^{escaped}\s*[、.．]?\s*", "", clean).strip()
    return clean


def normalize_outline_title(outline: dict | None) -> dict:
    if not isinstance(outline, dict):
        return outline or {}
    normalized = dict(outline)
    normalized["title"] = clean_outline_title(str(normalized.get("title") or ""))
    sections = []
    for section in normalized.get("sections") or []:
        if not isinstance(section, dict):
            continue
        clean_section = dict(section)
        clean_section["title"] = clean_outline_item_title(
            clean_section.get("title", ""),
            clean_section.get("index"),
        )
        if _is_meta_outline_title(clean_section["title"]):
            continue
        subsections = []
        for subsection in clean_section.get("subsections") or []:
            if not isinstance(subsection, dict):
                continue
            clean_subsection = dict(subsection)
            clean_subsection["title"] = clean_outline_item_title(
                clean_subsection.get("title", ""),
                clean_subsection.get("index"),
            )
            if _is_meta_outline_title(clean_subsection["title"]):
                continue
            subsections.append(clean_subsection)
        clean_section["subsections"] = subsections
        sections.append(clean_section)
    if "sections" in normalized:
        normalized["sections"] = sections
    return normalized


def _outline_title_with_original_index(index: Any, title: str) -> str:
    clean_title = str(title or "").strip()
    if not clean_title:
        return ""
    clean_title = clean_outline_item_title(clean_title, index)
    if re.match(r"^([一二三四五六七八九十]+、|\d+(?:\.\d+)*[、.]|第[一二三四五六七八九十\d]+[章节部分])", clean_title):
        return clean_title
    clean_index = str(index or "").strip()
    return f"{clean_index} {clean_title}".strip() if clean_index else clean_title


def format_outline_for_report(outline: dict | None) -> str:
    if not outline:
        return "无"

    title = str(outline.get("title") or "报告").strip()
    if _is_meta_outline_title(title):
        title = "报告"
    lines = [f"报告标题：{title}", "章节结构："]

    sections = outline.get("sections") or []
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_title = str(section.get("title") or "").strip()
        if not section_title or _is_meta_outline_title(section_title):
            continue
        rendered_section_title = _outline_title_with_original_index(section.get("index"), section_title)
        lines.append(f"- {rendered_section_title}")
        for subsection in section.get("subsections") or []:
            if not isinstance(subsection, dict):
                continue
            subsection_title = str(subsection.get("title") or "").strip()
            if not subsection_title or _is_meta_outline_title(subsection_title):
                continue
            rendered_subsection_title = _outline_title_with_original_index(
                subsection.get("index"), subsection_title
            )
            lines.append(f"  - {rendered_subsection_title}")

    if len(lines) == 2:
        lines.append("- 正文")
    return "\n".join(lines)


def extract_used_ids(text: str) -> tuple[str, set[str]]:
    used_ids: set[str] = set()
    match = USED_RE.search(text or "")
    if match:
        try:
            used_ids = {str(item) for item in json.loads(match.group(1))}
        except Exception:
            used_ids = set()
    clean = USED_RE.sub("", text or "").rstrip()
    return clean, used_ids


def filter_used_chunks(
    chunks: list[ProvidedChunkInput],
    answer_text: str,
    explicit_ids: set[str] | None = None,
) -> list[ProvidedChunkInput]:
    if not chunks:
        return []
    ids = explicit_ids or set()
    if ids:
        selected = [chunk for chunk in chunks if chunk.chunk_id in ids]
        return selected or chunks
    answer_chars = set(answer_text)
    used = []
    for chunk in chunks:
        content_chars = set(chunk.content)
        if content_chars and len(answer_chars & content_chars) / len(content_chars) >= 0.15:
            used.append(chunk)
    # The model may omit or mangle the hidden reference marker. In a RAG path,
    # retrieved chunks are the safest user-facing fallback.
    return used or chunks


def parse_outline_from_text(text: str) -> dict:
    match = JSON_BLOCK_RE.search(text or "")
    if match:
        parsed = parse_json_object(match.group(1))
        if parsed.get("title") is not None:
            return normalize_outline_title(parsed)
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    title = "报告大纲"
    sections: list[dict[str, Any]] = []
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
        elif line.startswith("## "):
            raw = line[3:].strip()
            m = re.match(r"(\d+)\.\s*(.+)", raw)
            index = int(m.group(1)) if m else len(sections) + 1
            sec_title = m.group(2).strip() if m else raw
            sections.append({"index": index, "title": sec_title, "subsections": []})
        elif line.startswith("### ") and sections:
            raw = line[4:].strip()
            m = re.match(r"([\d.]+)\s*(.+)", raw)
            subsections = sections[-1]["subsections"]
            subsections.append(
                {
                    "index": m.group(1).strip() if m else f"{sections[-1]['index']}.{len(subsections) + 1}",
                    "title": m.group(2).strip() if m else raw,
                }
            )
    return normalize_outline_title({"title": title, "sections": sections})


async def collect_stream(
    messages: list[dict[str, str]],
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    emitter=None,
    content_event: str | None = None,
) -> tuple[str, dict]:
    provider = get_provider("doubao")
    usage: dict = {}
    text = ""
    content_buffer = ""
    suppress_content = False

    async def emit_content_delta(delta: str):
        if not emitter or not content_event or not delta:
            return
        if content_event == "text_delta":
            await emitter.emit_text_delta(delta)
        elif content_event == "report_text_delta":
            await emitter.emit_report_text_delta(delta)
        elif content_event == "outline_delta":
            await emitter.emit_outline_delta(delta)

    async def emit_content_chunked(delta: str):
        remaining = delta
        while remaining:
            if len(remaining) <= CONTENT_CHUNK_TARGET_CHARS:
                await emit_content_delta(remaining)
                return
            window = remaining[:CONTENT_CHUNK_TARGET_CHARS]
            boundary = max(
                window.rfind("\n\n"),
                window.rfind("。"),
                window.rfind("；"),
                window.rfind("！"),
                window.rfind("？"),
                window.rfind(". "),
                window.rfind("? "),
                window.rfind("! "),
            )
            if boundary < CONTENT_CHUNK_MIN_CHARS:
                boundary = CONTENT_CHUNK_TARGET_CHARS
                step = boundary
            else:
                step = boundary + 1
            await emit_content_delta(remaining[:step])
            remaining = remaining[step:]

    async def flush_content(*, final: bool = False):
        nonlocal content_buffer, suppress_content
        if not emitter or not content_event or not content_buffer or suppress_content:
            return

        marker_index = content_buffer.find(USED_MARKER_PREFIX)
        if marker_index >= 0:
            before_marker = content_buffer[:marker_index]
            content_buffer = ""
            suppress_content = True
            if before_marker:
                await emit_content_chunked(before_marker)
            return

        if final:
            clean = USED_RE.sub("", content_buffer)
            content_buffer = ""
            if clean:
                await emit_content_chunked(clean)
            return

        # Keep a small suffix so a marker split across SDK chunks is not leaked.
        safe_len = max(0, len(content_buffer) - (len(USED_MARKER_PREFIX) - 1))
        if safe_len < CONTENT_CHUNK_MIN_CHARS:
            return
        safe_text = content_buffer[:safe_len]
        boundary = max(
            safe_text.rfind("\n\n"),
            safe_text.rfind("。"),
            safe_text.rfind("；"),
            safe_text.rfind("！"),
            safe_text.rfind("？"),
            safe_text.rfind(". "),
            safe_text.rfind("? "),
            safe_text.rfind("! "),
        )
        emit_len = boundary + 1 if boundary >= CONTENT_CHUNK_MIN_CHARS else safe_len
        delta = content_buffer[:emit_len]
        content_buffer = content_buffer[emit_len:]
        await emit_content_chunked(delta)

    async for item in provider.chat_stream(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    ):
        # Native model reasoning can include self-corrections, IDs, and reference markers.
        # Keep it for model quality at the provider level, but do not expose it to clients.
        delta = item.get("delta") or ""
        text += delta
        if emitter and content_event and delta and not suppress_content:
            content_buffer += delta
            await flush_content()
        if item.get("usage"):
            usage = item["usage"]
    await flush_content(final=True)
    return text, usage


async def emit_outline_in_deltas(emitter, text: str):
    if not emitter:
        return
    markdown = JSON_BLOCK_RE.sub("", text or "").rstrip()
    midpoint = max(1, len(markdown) // 2)
    for delta in (markdown[:midpoint], markdown[midpoint:]):
        if delta:
            await emitter.emit_outline_delta(delta)
