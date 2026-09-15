import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .exceptions import AppError, DependencyError, ParseError
from .node_factory import NodeFactory


class _ThreadRateLimiter:
    def __init__(self, rate_limit_per_sec: float) -> None:
        self.interval = 1.0 / max(0.000001, float(rate_limit_per_sec))
        self.lock = threading.Lock()
        self.next_allowed_ts = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_allowed_ts - now)
            self.next_allowed_ts = max(now, self.next_allowed_ts) + self.interval
        if delay > 0:
            time.sleep(delay)


_rate_limiters: dict[tuple[str, float], _ThreadRateLimiter] = {}
_rate_limiters_lock = threading.Lock()


def _get_thread_rate_limiter(name: str, rate_limit_per_sec: float) -> _ThreadRateLimiter | None:
    rate = float(rate_limit_per_sec)
    if rate <= 0:
        return None
    key = (name, round(rate, 6))
    with _rate_limiters_lock:
        limiter = _rate_limiters.get(key)
        if limiter is None:
            limiter = _ThreadRateLimiter(rate)
            _rate_limiters[key] = limiter
        return limiter

from .text_utils import (
    cap_pdf_tree_depth,
    clean_text,
    compact_docx_tree,
    detect_docx_heading_level,
    detect_pdf_heading_level,
    detect_text_encoding,
    looks_like_toc_entry,
    normalize_node_title,
    verify_docx_title_page_alignment,
)


class DocumentParser:
    """Parse multi-format documents into normalized node trees."""

    def __init__(
        self,
        opt: Any,
        add_node_id: bool,
        add_node_text: bool,
        add_doc_description: bool,
        max_file_size_mb: int = 200,
        txt_lines_per_node: int = 200,
        txt_chars_per_node: int = 1500,
        max_xlsx_rows_per_sheet: Optional[int] = 50000,
        xlsx_batch_size: int = 1000,
        summary_input_chars: int = 4000,
        pdf_max_depth: int = 2,
        pdf_parser: str = "auto",
        pdf_vision_model: Optional[str] = None,
        pdf_vision_concurrency: int = 3,
        pdf_table_mode: str = "auto",
    ) -> None:
        self.opt = opt
        self.add_node_id = bool(add_node_id)
        self.add_node_text = bool(add_node_text)
        self.add_doc_description = bool(add_doc_description)
        self.node_factory = NodeFactory(add_node_id=self.add_node_id, add_node_text=self.add_node_text)
        self.max_file_size_mb = max_file_size_mb
        self.txt_lines_per_node = max(1, int(txt_lines_per_node))
        self.txt_chars_per_node = max(100, int(txt_chars_per_node))
        self.max_xlsx_rows_per_sheet = max_xlsx_rows_per_sheet
        self.xlsx_batch_size = max(1, int(xlsx_batch_size))
        self.summary_input_chars = max(100, int(summary_input_chars))
        self.pdf_max_depth = max(1, int(pdf_max_depth))
        self.pdf_parser = str(pdf_parser or "auto").strip().lower()
        self.pdf_vision_model = clean_text(
            pdf_vision_model or os.getenv("ARK_VISION_MODEL") or os.getenv("ARK_MODEL")
        )
        self.pdf_vision_concurrency = max(1, int(pdf_vision_concurrency or 1))
        self.pdf_vision_rate_limit_per_sec = max(
            0.0,
            float(
                os.getenv("INGEST_PDF_VISION_RATE_LIMIT_PER_SEC")
                or os.getenv("ARK_VISION_RATE_LIMIT_PER_SEC")
                or "1.0"
            ),
        )
        self._pdf_vision_rate_limiter = _get_thread_rate_limiter(
            "ark_vision",
            self.pdf_vision_rate_limit_per_sec,
        )
        self.pdf_table_mode = str(pdf_table_mode or "auto").strip().lower()

        self.async_supported_formats: Dict[str, Any] = {
            "pdf": self._process_pdf_async,
            "docx": self._process_docx_async,
            "xlsx": self._process_xlsx_async,
            "txt": self._process_txt_async,
            "pptx": self._process_pptx_async,
        }

    @staticmethod
    def _format_timestamp(ts: float) -> str:
        """Format epoch timestamp as local datetime string."""
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    def _check_file_size(self, file_path: str) -> None:
        """Fail fast when the input file exceeds configured size limit."""
        if self.max_file_size_mb is None or self.max_file_size_mb <= 0:
            return

        file_size = os.path.getsize(file_path)
        max_bytes = int(self.max_file_size_mb * 1024 * 1024)
        if file_size > max_bytes:
            raise ValueError(
                f"File too large: {file_size / (1024 * 1024):.2f} MB > {self.max_file_size_mb} MB. "
                "Please increase --max-file-size-mb or split the document."
            )

    @staticmethod
    def get_file_type(file_path: str) -> str:
        """Return normalized extension without leading dot."""
        return Path(file_path).suffix.lower().lstrip(".")

    async def process(self, file_path: str) -> Dict[str, Any]:
        """Dispatch one file to the matching async parser."""
        self._check_file_size(file_path)
        file_type = self.get_file_type(file_path)
        if file_type not in self.async_supported_formats:
            raise ValueError(
                f"Unsupported file format: {file_type}, supported: {list(self.async_supported_formats.keys())}"
            )
        return await self.async_supported_formats[file_type](file_path)

    async def _process_pdf_async(self, file_path: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self.process_pdf, file_path)

    async def _process_docx_async(self, file_path: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self.process_docx, file_path)

    async def _process_xlsx_async(self, file_path: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self.process_xlsx, file_path)

    async def _process_txt_async(self, file_path: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self.process_txt, file_path)

    async def _process_pptx_async(self, file_path: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self.process_pptx, file_path)

    def _create_base_node(
        self,
        node_id: int,
        text: str,
        page_number: Optional[int],
        level: int,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """Create one normalized node through shared NodeFactory."""
        return self.node_factory.create(
            node_id=node_id,
            text=text,
            page_number=page_number,
            level=level,
            **kwargs,
        )

    def _generate_doc_description(self, structure: Dict[str, Any]) -> str:
        """Generate lightweight document-level description text."""
        node_count = len(structure.get("nodes", []))
        return f"{structure.get('doc_name', 'unknown')} ({structure.get('doc_type', 'unknown')}), nodes: {node_count}"

    def enrich_with_metadata(self, structure: Any, file_path: str, doc_type: str) -> Dict[str, Any]:
        """Attach metadata and optional document description to output structure."""
        if not isinstance(structure, dict):
            structure = {"nodes": structure}

        created_at = None
        modified_at = None
        if os.path.exists(file_path):
            created_at = self._format_timestamp(os.path.getctime(file_path))
            modified_at = self._format_timestamp(os.path.getmtime(file_path))

        structure.update(
            {
                "doc_type": doc_type,
                "doc_name": os.path.basename(file_path),
                "file_path": os.path.abspath(file_path),
                "created_at": created_at,
                "modified_at": modified_at,
            }
        )

        if self.add_doc_description:
            structure["doc_description"] = self._generate_doc_description(structure)

        if "status" not in structure:
            structure["status"] = "ok"

        return structure

    def _error_result(self, doc_type: str, file_path: str, error: AppError) -> Dict[str, Any]:
        """Build normalized error payload for one file."""
        logging.exception("Failed to process %s in stage=%s: %s", file_path, error.stage, error)
        payload = {
            "status": "error",
            "doc_type": doc_type,
            "doc_name": os.path.basename(file_path),
            "nodes": [],
            "error": {
                "type": error.__class__.__name__,
                "message": str(error),
                "stage": error.stage,
                "retryable": error.retryable,
            },
        }
        return self.enrich_with_metadata(payload, file_path, doc_type)

    def _extract_pdf_pages_with_pypdf2(self, pdf_path: str) -> List[Tuple[int, str]]:
        """Extract plain text content per PDF page."""
        try:
            import PyPDF2
        except ImportError as e:
            raise DependencyError("Missing dependency PyPDF2. Install: pip install PyPDF2", cause=e)

        with open(pdf_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            pages: List[Tuple[int, str]] = []
            for idx, page in enumerate(reader.pages, 1):
                text = clean_text(page.extract_text() or "")
                pages.append((idx, text))
        return pages

    def _extract_pdf_pages_with_pymupdf(self, pdf_path: str) -> List[Tuple[int, str]]:
        """Extract plain text content per PDF page with PyMuPDF."""
        try:
            import fitz
        except ImportError as e:
            raise DependencyError("Missing dependency PyMuPDF. Install: pip install PyMuPDF", cause=e) from e

        doc = None
        try:
            doc = fitz.open(pdf_path)
            pages: List[Tuple[int, str]] = []
            for idx, page in enumerate(doc, 1):
                text = clean_text(page.get_text("text") or "")
                pages.append((idx, text))
            return pages
        finally:
            if doc is not None:
                doc.close()


    @staticmethod
    def _extract_json_text(content: str) -> str:
        """Extract OCR text from a JSON response, falling back to raw content."""
        content = clean_text(content)
        if not content:
            return ""
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(content[start : end + 1])
                return clean_text(data.get("text") or data.get("content") or "")
            except Exception:
                pass
        return content

    @staticmethod
    def _looks_like_multicolumn(text_blocks: List[Any], page_width: float) -> bool:
        """Heuristically detect two-column page layouts from text block centers."""
        if len(text_blocks) < 8 or page_width <= 0:
            return False
        centers = []
        for block in text_blocks:
            try:
                x0, _, x1, _ = block[:4]
                centers.append((float(x0) + float(x1)) / 2)
            except Exception:
                continue
        left = [x for x in centers if x < page_width * 0.45]
        right = [x for x in centers if x > page_width * 0.55]
        return len(left) >= 4 and len(right) >= 4

    def _scan_pdf_page_complexity(self, pdf_path: str, max_pages: Optional[int] = None) -> Dict[str, Any]:
        """Use PyMuPDF metadata to decide whether a PDF needs enhanced parsing."""
        try:
            import fitz
        except ImportError as e:
            raise DependencyError("Missing dependency PyMuPDF. Install: pip install PyMuPDF", cause=e) from e

        doc = None
        page_results: List[Dict[str, Any]] = []
        try:
            doc = fitz.open(pdf_path)
            total_pages = len(doc)
            scan_pages = min(total_pages, max_pages or total_pages)
            for page_idx in range(scan_pages):
                page = doc[page_idx]
                rect = page.rect
                page_area = float(rect.width * rect.height) or 1.0

                text = page.get_text("text") or ""
                text_chars = len(clean_text(text))
                raw_blocks = page.get_text("blocks") or []
                text_blocks = [b for b in raw_blocks if len(b) >= 5 and clean_text(str(b[4]))]

                image_count = 0
                image_area = 0.0
                for img in page.get_images(full=True):
                    image_count += 1
                    xref = img[0]
                    try:
                        for bbox in page.get_image_rects(xref):
                            image_area += float(bbox.width * bbox.height)
                    except Exception:
                        continue
                image_area_ratio = image_area / page_area

                drawings = page.get_drawings()
                horizontal_lines = 0
                vertical_lines = 0
                for drawing in drawings:
                    for item in drawing.get("items", []):
                        if not item:
                            continue
                        if item[0] == "l" and len(item) >= 3:
                            p1, p2 = item[1], item[2]
                            dx = abs(float(p1.x) - float(p2.x))
                            dy = abs(float(p1.y) - float(p2.y))
                            if dx > 20 and dy < 2:
                                horizontal_lines += 1
                            if dy > 20 and dx < 2:
                                vertical_lines += 1
                        elif item[0] == "re" and len(item) >= 2:
                            rect_item = item[1]
                            width = abs(float(rect_item.x1) - float(rect_item.x0))
                            height = abs(float(rect_item.y1) - float(rect_item.y0))
                            if width > 20 and height < 2:
                                horizontal_lines += 1
                            if height > 20 and width < 2:
                                vertical_lines += 1

                table_line_count = horizontal_lines + vertical_lines
                has_grid_lines = horizontal_lines >= 3 and vertical_lines >= 3 and table_line_count >= 8
                multi_column = self._looks_like_multicolumn(text_blocks, float(rect.width))

                score = 0
                reasons: List[str] = []
                ocr_like = False
                ppt_like = False

                if text_chars < 30 and image_count > 0:
                    score += 4
                    ocr_like = True
                    reasons.append("ocr_like_image_page")
                if image_area_ratio > 0.4:
                    score += 2
                    reasons.append("high_image_area")
                if len(drawings) > 30:
                    score += 2
                    reasons.append("many_drawings")
                if len(text_blocks) > 40:
                    score += 1
                    reasons.append("many_text_blocks")
                if multi_column:
                    score += 1
                    reasons.append("multi_column")
                if table_line_count > 20 or has_grid_lines:
                    reasons.append("table_like_lines")
                if text_chars < 300 and (image_count > 0 or len(drawings) > 20):
                    score += 2
                    ppt_like = True
                    reasons.append("ppt_like_sparse_text_visual_page")

                page_results.append(
                    {
                        "page": page_idx + 1,
                        "text_chars": text_chars,
                        "text_block_count": len(text_blocks),
                        "image_count": image_count,
                        "image_area_ratio": round(image_area_ratio, 3),
                        "drawing_count": len(drawings),
                        "table_line_count": table_line_count,
                        "multi_column": multi_column,
                        "ocr_like": ocr_like,
                        "ppt_like": ppt_like,
                        "score": score,
                        "is_complex": score >= 4,
                        "reasons": reasons,
                    }
                )

            scanned = len(page_results) or 1
            complex_pages = [p for p in page_results if p["is_complex"]]
            ocr_pages = [p for p in page_results if p["ocr_like"]]
            ppt_pages = [p for p in page_results if p["ppt_like"]]
            table_pages = [
                p for p in page_results
                if p["table_line_count"] > 20
                or "table_like_lines" in (p.get("reasons") or [])
            ]
            total_text_chars = sum(int(p["text_chars"]) for p in page_results)

            decision = "pymupdf"
            reason = "simple_text_pdf"
            if len(ocr_pages) / scanned >= 0.5:
                decision = "ark_vision"
                reason = "ocr_pdf"
            elif len(complex_pages) / scanned >= 0.3:
                decision = "ark_vision"
                reason = "complex_layout_pdf"
            elif len(table_pages) / scanned >= 0.2:
                reason = "text_pdf_with_tables"

            return {
                "pages_scanned": scanned,
                "page_count": total_pages,
                "complex_pages": len(complex_pages),
                "ocr_pages": len(ocr_pages),
                "ppt_like_pages": len(ppt_pages),
                "table_like_pages": len(table_pages),
                "total_text_chars": total_text_chars,
                "decision": decision,
                "reason": reason,
                "page_details": page_results,
            }
        finally:
            if doc is not None:
                doc.close()

    @staticmethod
    def _looks_like_table_by_words(page: Any) -> Tuple[bool, Dict[str, Any]]:
        """Fast, generic table-page detection based on word coordinates."""
        try:
            words = page.get_text("words") or []
            page_width = float(page.rect.width) or 1.0
        except Exception:
            return False, {}

        clean_words: List[Tuple[float, float, float, float, str]] = []
        numeric_count = 0
        for word in words:
            if len(word) < 5:
                continue
            text = clean_text(str(word[4]))
            if not text:
                continue
            try:
                x0, y0, x1, y1 = float(word[0]), float(word[1]), float(word[2]), float(word[3])
            except Exception:
                continue
            clean_words.append((x0, y0, x1, y1, text))
            if re.search(r"\d", text):
                numeric_count += 1

        if len(clean_words) < 24:
            return False, {
                "word_count": len(clean_words),
                "table_like_rows": 0,
                "stable_columns": 0,
                "numeric_ratio": 0.0,
            }

        rows: List[List[Tuple[float, float, float, float, str]]] = []
        for item in sorted(clean_words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
            y_center = (item[1] + item[3]) / 2
            if rows:
                last = rows[-1]
                last_y = sum((w[1] + w[3]) / 2 for w in last) / len(last)
                if abs(y_center - last_y) <= 4:
                    last.append(item)
                    continue
            rows.append([item])

        table_like_rows = 0
        table_like_cell_counts: List[int] = []
        column_bins: Dict[int, int] = {}
        for row in rows:
            row = sorted(row, key=lambda w: w[0])
            if len(row) < 3:
                continue
            span = max(w[2] for w in row) - min(w[0] for w in row)
            gaps = [row[i + 1][0] - row[i][2] for i in range(len(row) - 1)]
            large_gap_indexes = [idx for idx, gap in enumerate(gaps) if gap > 24]
            segment_starts = [row[0][0]] + [row[idx + 1][0] for idx in large_gap_indexes]
            segment_count = len(segment_starts)

            # A table row has separated cell-like segments; a prose line merely has many words.
            if span > page_width * 0.28 and segment_count >= 3:
                table_like_rows += 1
                table_like_cell_counts.append(segment_count)
                for x0 in segment_starts:
                    column_key = int(round(x0 / 18.0) * 18)
                    column_bins[column_key] = column_bins.get(column_key, 0) + 1

        stable_threshold = max(4, int(table_like_rows * 0.4))
        stable_columns = sum(1 for count in column_bins.values() if count >= stable_threshold)
        numeric_ratio = numeric_count / max(1, len(clean_words))
        avg_cells_per_table_row = (
            sum(table_like_cell_counts) / len(table_like_cell_counts)
            if table_like_cell_counts
            else 0.0
        )

        looks_like = (
            table_like_rows >= 8 and stable_columns >= 4 and avg_cells_per_table_row >= 4
        ) or (
            table_like_rows >= 10 and stable_columns >= 3 and numeric_ratio >= 0.16
        ) or (
            table_like_rows >= 12 and stable_columns >= 2 and numeric_ratio >= 0.25
        )
        return looks_like, {
            "word_count": len(clean_words),
            "table_like_rows": table_like_rows,
            "stable_columns": stable_columns,
            "numeric_ratio": round(numeric_ratio, 3),
            "avg_cells_per_table_row": round(avg_cells_per_table_row, 2),
        }

    def _detect_pdf_table_pages(self, pdf_path: str) -> List[int]:
        """Detect likely table pages for lightweight table enhancement."""
        try:
            import fitz
        except ImportError as e:
            raise DependencyError("Missing dependency PyMuPDF. Install: pip install PyMuPDF", cause=e) from e

        doc = None
        table_pages: List[int] = []
        started_at = time.perf_counter()
        try:
            doc = fitz.open(pdf_path)
            logging.info("PDF table page scan start: source=%s pages=%s", pdf_path, len(doc))
            for page_idx, page in enumerate(doc, 1):
                reasons: List[str] = []

                drawings = page.get_drawings()
                horizontal_lines = 0
                vertical_lines = 0
                for drawing in drawings:
                    for item in drawing.get("items", []):
                        if not item:
                            continue
                        if item[0] == "l" and len(item) >= 3:
                            p1, p2 = item[1], item[2]
                            dx = abs(float(p1.x) - float(p2.x))
                            dy = abs(float(p1.y) - float(p2.y))
                            if dx > 20 and dy < 2:
                                horizontal_lines += 1
                            if dy > 20 and dx < 2:
                                vertical_lines += 1
                        elif item[0] == "re" and len(item) >= 2:
                            rect_item = item[1]
                            width = abs(float(rect_item.x1) - float(rect_item.x0))
                            height = abs(float(rect_item.y1) - float(rect_item.y0))
                            if width > 20 and height < 2:
                                horizontal_lines += 1
                            if height > 20 and width < 2:
                                vertical_lines += 1
                table_line_count = horizontal_lines + vertical_lines
                has_grid_lines = horizontal_lines >= 3 and vertical_lines >= 3 and table_line_count >= 8
                if table_line_count > 20 or has_grid_lines:
                    reasons.append(f"table_lines={table_line_count},h={horizontal_lines},v={vertical_lines}")

                word_table, word_metrics = self._looks_like_table_by_words(page)
                if word_table:
                    reasons.append(
                        "word_grid="
                        f"rows:{word_metrics.get('table_like_rows')},"
                        f"cols:{word_metrics.get('stable_columns')},"
                        f"num:{word_metrics.get('numeric_ratio')},"
                        f"cells:{word_metrics.get('avg_cells_per_table_row')}"
                    )

                text = clean_text(page.get_text("text") or "")
                numeric_tokens = re.findall(r"\d[\d,.%]*", text)
                text_blocks = [b for b in (page.get_text("blocks") or []) if len(b) >= 5 and clean_text(str(b[4]))]
                if (
                    len(numeric_tokens) >= 70
                    and len(text_blocks) >= 18
                    and (
                        word_metrics.get("stable_columns", 0) >= 3
                        or word_metrics.get("table_like_rows", 0) >= 10
                    )
                ):
                    reasons.append(f"dense_numbers={len(numeric_tokens)}")

                if reasons:
                    table_pages.append(page_idx)

                if page_idx % 25 == 0 or page_idx == len(doc):
                    logging.info(
                        "PDF table page scan progress: page=%s/%s detected=%s elapsed_ms=%s",
                        page_idx,
                        len(doc),
                        len(table_pages),
                        int((time.perf_counter() - started_at) * 1000),
                    )

            logging.info(
                "PDF table page scan done: source=%s table_pages=%s pages=%s elapsed_ms=%s",
                pdf_path,
                len(table_pages),
                table_pages[:30],
                int((time.perf_counter() - started_at) * 1000),
            )
            return table_pages
        finally:
            if doc is not None:
                doc.close()

    def _enhance_pdf_table_pages_with_vision(
        self,
        pdf_path: str,
        pages: List[Tuple[int, str]],
    ) -> List[Tuple[int, str]]:
        """Append Ark Vision table parsing results to PyMuPDF text pages."""
        if self.pdf_table_mode == "off":
            return pages

        table_pages = self._detect_pdf_table_pages(pdf_path)
        if not table_pages:
            return pages

        if not self.pdf_vision_model:
            logging.warning("PDF table enhancement skipped: ARK_VISION_MODEL/pdf_vision_model is not configured")
            return pages
        if not clean_text(os.getenv("ARK_API_KEY")):
            logging.warning("PDF table enhancement skipped: ARK_API_KEY is not configured")
            return pages

        prompt = (
            "请只提取这页里的表格内容。要求：1.表格必须输出为Markdown表格；"
            "2.保留表头、行名、单位和关键数值；3.如果没有表格，返回空字符串；"
            "4.不要解释，不要总结，只输出JSON，格式为{\"text\":\"...\"}。"
        )
        logging.info("PDF table enhancement start: pages=%s mode=%s", len(table_pages), self.pdf_table_mode)
        enhanced = dict(self._extract_pdf_pages_with_ark_vision(pdf_path, page_numbers=table_pages, prompt=prompt))
        if not enhanced:
            return pages

        merged_pages: List[Tuple[int, str]] = []
        for page_num, text in pages:
            table_text = clean_text(enhanced.get(page_num, ""))
            if table_text:
                text = clean_text(f"{text}\n\n[表格增强解析]\n{table_text}")
            merged_pages.append((page_num, text))

        logging.info("PDF table enhancement done: enhanced_pages=%s", len(enhanced))
        return merged_pages


    def _extract_pdf_pages_with_ark_vision(
        self,
        pdf_path: str,
        page_numbers: Optional[List[int]] = None,
        prompt: Optional[str] = None,
    ) -> List[Tuple[int, str]]:
        """Render each PDF page and ask Ark vision model to extract text/table content."""
        try:
            import fitz
            from volcenginesdkarkruntime import Ark
        except ImportError as e:
            raise DependencyError(
                "Missing dependency for Ark vision OCR. Install: pip install volcengine-python-sdk[ark] PyMuPDF",
                stage="pdf_ark_vision",
                cause=e,
            ) from e

        api_key = clean_text(os.getenv("ARK_API_KEY"))
        base_url = clean_text(os.getenv("ARK_BASE_URL")) or "https://ark.cn-beijing.volces.com/api/v3"
        if not api_key:
            raise DependencyError("ARK_API_KEY is not configured for Ark vision OCR.", stage="pdf_ark_vision")
        if not self.pdf_vision_model:
            raise DependencyError("ARK_VISION_MODEL/pdf_vision_model is not configured.", stage="pdf_ark_vision")

        prompt = prompt or (
            "请对这页文档图片做OCR和版面解析，只提取文字与表格，不解释图片内容。"
            "要求：1.按自然阅读顺序输出；2.普通文字输出为段落；"
            "3.表格输出为Markdown表格；4.忽略装饰性图片；"
            "5.只输出JSON，格式为{\"text\":\"...\"}。"
        )

        doc = None
        temp_dir = Path(tempfile.mkdtemp(prefix="ark_vision_pdf_"))
        started_at = time.perf_counter()
        try:
            doc = fitz.open(pdf_path)
            selected_pages = set(page_numbers or [])
            rendered_pages: List[Tuple[int, Path]] = []
            for page_idx, page in enumerate(doc, 1):
                if selected_pages and page_idx not in selected_pages:
                    continue
                pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                image_path = temp_dir / f"page_{page_idx}.jpg"
                pix.save(str(image_path), jpg_quality=85)
                rendered_pages.append((page_idx, image_path))
            if not rendered_pages:
                return []

            def _ocr_one(page_idx: int, image_path: Path) -> Tuple[int, str]:
                page_started = time.perf_counter()
                image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
                image_url = f"data:image/jpeg;base64,{image_b64}"
                client = Ark(api_key=api_key, base_url=base_url)

                if self._pdf_vision_rate_limiter is not None:
                    self._pdf_vision_rate_limiter.wait()
                resp = client.chat.completions.create(
                    model=self.pdf_vision_model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": image_url}},
                                {"type": "text", "text": prompt},
                            ],
                        }
                    ],
                    temperature=0,
                    max_tokens=2048,
                    thinking={"type": "disabled"},
                )
                content = clean_text(resp.choices[0].message.content)
                text = self._extract_json_text(content)
                logging.info(
                    "Ark vision OCR page done: page=%s chars=%s elapsed_ms=%s",
                    page_idx,
                    len(text),
                    int((time.perf_counter() - page_started) * 1000),
                )
                return page_idx, text

            pages: List[Tuple[int, str]] = []
            worker_count = min(self.pdf_vision_concurrency, len(rendered_pages)) or 1
            logging.info(
                "Ark vision OCR concurrency: workers=%s pages=%s",
                worker_count,
                len(rendered_pages),
            )
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [
                    executor.submit(_ocr_one, page_idx, image_path)
                    for page_idx, image_path in rendered_pages
                ]
                for future in as_completed(futures):
                    pages.append(future.result())

            pages.sort(key=lambda item: item[0])

            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logging.info(
                "Ark vision PDF parse done: source=%s model=%s pages=%s concurrency=%s elapsed_ms=%s",
                pdf_path,
                self.pdf_vision_model,
                len(pages),
                worker_count,
                elapsed_ms,
            )
            return pages
        finally:
            if doc is not None:
                doc.close()
            shutil.rmtree(temp_dir, ignore_errors=True)


    def _extract_pdf_toc_entries_with_pypdf2(self, pdf_path: str) -> List[Dict[str, Any]]:
        """Extract outline entries from PDF TOC with iterative traversal."""
        try:
            import PyPDF2
        except ImportError:
            return []

        with open(pdf_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            raw_outline = getattr(reader, "outline", None)
            if raw_outline is None:
                raw_outline = getattr(reader, "outlines", None)
            if not raw_outline:
                return []

            entries: List[Dict[str, Any]] = []

            def _walk(items: Any, level: int) -> None:
                if not isinstance(items, list):
                    items = [items]

                for item in items:
                    if isinstance(item, list):
                        _walk(item, level + 1)
                        continue

                    title = clean_text(getattr(item, "title", None))
                    if not title and isinstance(item, dict):
                        title = clean_text(item.get("/Title"))
                    if not title:
                        continue

                    page_number: Optional[int] = None
                    try:
                        page_number = int(reader.get_destination_page_number(item)) + 1
                    except Exception:
                        page_number = None

                    if isinstance(page_number, int) and page_number >= 1:
                        entries.append({"title": title, "page": page_number, "level": max(1, int(level))})

            _walk(raw_outline, 1)
            return entries

    def _extract_pdf_headings_by_regex(self, pages: List[Tuple[int, str]]) -> List[Dict[str, Any]]:
        """Infer heading candidates from page text when TOC is unavailable."""
        headings: List[Dict[str, Any]] = []
        for page_num, page_text in pages:
            if not page_text:
                continue
            per_page_count = 0
            raw_lines = page_text.splitlines()
            idx = 0
            while idx < len(raw_lines):
                line = clean_text(raw_lines[idx])
                if re.fullmatch(r"[0-9]{1,2}(?:\.[0-9]{1,2})?", line or "") and idx + 1 < len(raw_lines):
                    next_line = clean_text(raw_lines[idx + 1])
                    if (
                        1 <= len(next_line) <= 60
                        and re.match(r"^[\u4e00-\u9fffA-Z]", next_line)
                        and not re.search(r"[。；;，,]$", next_line)
                        and not re.match(r"^[（(［\[\]）),.;:：，。]", next_line)
                        and not looks_like_toc_entry(next_line)
                    ):
                        line = clean_text(f"{line} {next_line}")
                        idx += 1
                idx += 1
                if not line:
                    continue
                if len(line) < 4 or len(line) > 120:
                    continue
                if looks_like_toc_entry(line):
                    continue

                level = detect_pdf_heading_level(line)
                if level is None:
                    continue

                per_page_count += 1
                if per_page_count > 8:
                    break
                headings.append({"title": line, "page": page_num, "level": level})

        deduped: List[Dict[str, Any]] = []
        seen_keys: set = set()
        for h in headings:
            key = (normalize_node_title(h["title"]), h["page"], h["level"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(h)
        return self._normalize_pdf_heading_levels(deduped)

    @staticmethod
    def _is_figure_table_toc_title(title: str) -> bool:
        """Return True when a TOC title is a figure/table style entry."""
        normalized = clean_text(title)
        if not normalized:
            return False

        if normalized.startswith("\u56fe\u8868"):
            remainder = normalized[2:].lstrip()
            return bool(remainder[:1].isdigit())

        if normalized.startswith(("\u56fe", "\u8868")):
            remainder = normalized[1:].lstrip()
            return bool(remainder[:1].isdigit())

        english_pattern = r"^(figure|fig\.?|table)\s*\d+"
        return re.match(english_pattern, normalized.lower()) is not None

    @staticmethod
    def _normalize_pdf_heading_levels(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Normalize heading levels to a compact, stable hierarchy across documents.

        Strategy:
        1) shift levels so minimum becomes 1;
        2) compress sparse level values (e.g. 1/3/5 -> 1/2/3);
        3) smooth large upward jumps (e.g. 1 -> 4 becomes 1 -> 2).
        """
        if not entries:
            return entries

        raw_levels: List[int] = []
        for item in entries:
            lv = item.get("level")
            if isinstance(lv, int) and lv >= 1:
                raw_levels.append(lv)
        if not raw_levels:
            return entries

        min_level = min(raw_levels)
        normalized_values = [max(1, lv - min_level + 1) for lv in raw_levels]
        uniq = sorted(set(normalized_values))
        rank_map = {old: idx + 1 for idx, old in enumerate(uniq)}

        idx = 0
        previous_level = 1
        for item in entries:
            lv = item.get("level")
            if not isinstance(lv, int) or lv < 1:
                continue
            compact_level = rank_map.get(normalized_values[idx], 1)
            idx += 1

            # Avoid extreme upward jumps that make hierarchy unstable.
            if compact_level > previous_level + 1:
                compact_level = previous_level + 1

            item["level"] = max(1, min(6, int(compact_level)))
            previous_level = item["level"]

        return entries

    def _build_pdf_nodes_from_entries(self, entries: List[Dict[str, Any]], pages: List[Tuple[int, str]]) -> List[Dict[str, Any]]:
        """Build hierarchical PDF nodes from title-page entries."""
        if not entries:
            return []

        page_count = len(pages)
        page_map: Dict[int, str] = {num: text for num, text in pages}
        normalized_entries: List[Dict[str, Any]] = []
        for item in entries:
            title = clean_text(item.get("title"))
            page = item.get("page")
            level = item.get("level", 1)
            if not title or not isinstance(page, int):
                continue
            if self._is_figure_table_toc_title(title):
                continue
            if page < 1 or page > page_count:
                continue
            normalized_entries.append({"title": title, "page": page, "level": max(1, int(level))})
        if not normalized_entries:
            return []

        first_page = normalized_entries[0]["page"]
        if first_page > 1:
            normalized_entries.insert(
                0,
                {
                    "title": "文档开头",
                    "page": 1,
                    "level": 1,
                },
            )

        for i, item in enumerate(normalized_entries):
            start_page = item["page"]
            end_page = page_count
            for j in range(i + 1, len(normalized_entries)):
                nxt = normalized_entries[j]
                if nxt["level"] <= item["level"] and nxt["page"] >= start_page:
                    end_page = max(start_page, nxt["page"] - 1)
                    break
            item["start_index"] = start_page
            item["end_index"] = end_page

        node_id = 1
        root_nodes: List[Dict[str, Any]] = []
        stack: List[Tuple[int, Dict[str, Any]]] = []

        for item in normalized_entries:
            start_page = item["start_index"]
            end_page = item["end_index"]
            range_text = clean_text("\n".join(page_map.get(p, "") for p in range(start_page, end_page + 1)))
            if not range_text:
                range_text = item["title"]

            node = self._create_base_node(
                node_id=node_id,
                text=range_text,
                page_number=start_page,
                level=item["level"],
                title=item["title"],
                start_index=start_page,
                end_index=end_page,
                nodes=[],
            )
            if node is None:
                continue
            node["_summary_source"] = range_text[: max(self.summary_input_chars * 2, 8000)]
            node_id += 1

            while stack and stack[-1][0] >= item["level"]:
                stack.pop()
            if stack:
                stack[-1][1].setdefault("nodes", []).append(node)
            else:
                root_nodes.append(node)
            stack.append((item["level"], node))

        return root_nodes

    def _build_pdf_page_nodes(self, pages: List[Tuple[int, str]]) -> List[Dict[str, Any]]:
        """Fallback PDF structure: one node per page."""
        nodes: List[Dict[str, Any]] = []
        node_id = 1
        for page_num, text in pages:
            node = self._create_base_node(
                node_id=node_id,
                text=text or f"Page {page_num}",
                page_number=page_num,
                level=1,
                title=f"Page {page_num}",
                start_index=page_num,
                end_index=page_num,
                nodes=[],
            )
            if node is not None:
                node["_summary_source"] = clean_text(text)[: max(self.summary_input_chars * 2, 8000)]
                nodes.append(node)
                node_id += 1
        return nodes

    def _build_pdf_sliding_window_nodes(
        self,
        pages: List[Tuple[int, str]],
        window_size: int = 2,
        stride: int = 1,
    ) -> List[Dict[str, Any]]:
        """Build flat nodes from overlapping page windows for OCR-style PDFs."""
        clean_pages = [(page_num, clean_text(text)) for page_num, text in pages if clean_text(text)]
        if not clean_pages:
            return []

        window_size = max(1, int(window_size))
        stride = max(1, int(stride))
        nodes: List[Dict[str, Any]] = []
        node_id = 1
        idx = 0
        while idx < len(clean_pages):
            window = clean_pages[idx : idx + window_size]
            if not window:
                break
            start_page = window[0][0]
            end_page = window[-1][0]
            text = clean_text("\n".join(part for _, part in window))
            title = f"Page {start_page}" if start_page == end_page else f"Page {start_page}-{end_page}"
            node = self._create_base_node(
                node_id=node_id,
                text=text or title,
                page_number=start_page,
                level=1,
                title=title,
                start_index=start_page,
                end_index=end_page,
                nodes=[],
            )
            if node is not None:
                node["_summary_source"] = text[: max(self.summary_input_chars * 2, 8000)]
                nodes.append(node)
                node_id += 1
            if idx + window_size >= len(clean_pages):
                break
            idx += stride
        return nodes

    @staticmethod
    def _resolve_soffice_binary() -> Optional[str]:
        """Resolve LibreOffice/soffice executable for headless DOCX conversion."""
        env_path = clean_text(os.getenv("SOFFICE_PATH"))
        if env_path:
            return env_path

        for candidate in ("soffice", "libreoffice"):
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return None

    def _convert_docx_to_pdf(self, docx_path: str) -> str:
        """Convert DOCX to PDF with LibreOffice headless mode and return temp PDF path."""
        soffice_bin = self._resolve_soffice_binary()
        if not soffice_bin:
            raise DependencyError(
                "LibreOffice/soffice is not available. Install libreoffice or set SOFFICE_PATH.",
                stage="docx_pdf_convert",
            )

        temp_dir = tempfile.mkdtemp(prefix="docx_pdf_")
        output_dir = Path(temp_dir)
        src_path = Path(docx_path).resolve()
        pdf_path = output_dir / f"{src_path.stem}.pdf"
        cmd = [
            soffice_bin,
            "--headless",
            "--convert-to",
            "pdf:writer_pdf_Export",
            "--outdir",
            str(output_dir),
            str(src_path),
        ]

        started_at = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise ParseError("LibreOffice DOCX->PDF conversion timed out", stage="docx_pdf_convert", cause=e) from e
        except OSError as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise ParseError(f"Failed to start LibreOffice converter: {e}", stage="docx_pdf_convert", cause=e) from e

        if proc.returncode != 0 or not pdf_path.is_file():
            stderr = clean_text(proc.stderr or "")
            stdout = clean_text(proc.stdout or "")
            details = stderr or stdout or f"exit_code={proc.returncode}"
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise ParseError(f"LibreOffice DOCX->PDF conversion failed: {details}", stage="docx_pdf_convert")

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        logging.info(
            "DOCX->PDF conversion done: source=%s elapsed_ms=%s output=%s",
            str(src_path),
            elapsed_ms,
            str(pdf_path),
        )
        return str(pdf_path)

    def _process_docx_via_pdf(self, docx_path: str) -> Dict[str, Any]:
        """Convert DOCX to PDF, then reuse the PDF parser for page-accurate structure."""
        temp_pdf_path = self._convert_docx_to_pdf(docx_path)
        temp_dir = str(Path(temp_pdf_path).parent)
        try:
            pdf_structure = self.process_pdf(temp_pdf_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if pdf_structure.get("status") != "ok":
            error_payload = pdf_structure.get("error") or {}
            raise ParseError(
                f"DOCX->PDF parse failed: {error_payload.get('message') or 'unknown error'}",
                stage="docx_pdf_pipeline",
            )

        pdf_structure["doc_type"] = "docx"
        pdf_structure["doc_name"] = os.path.basename(docx_path)
        return self.enrich_with_metadata(pdf_structure, docx_path, "docx")

    def process_pdf(self, pdf_path: str) -> Dict[str, Any]:
        """Parse PDF and output structured nodes with metadata."""
        started_at = time.perf_counter()
        try:
            parser_mode = self.pdf_parser
            complexity: Optional[Dict[str, Any]] = None
            if parser_mode == "auto":
                complexity = self._scan_pdf_page_complexity(pdf_path, max_pages=10)
                parser_mode = str(complexity.get("decision") or "pymupdf")
                logging.info(
                    "PDF complexity scan done: source=%s pages=%s scanned=%s complex=%s ocr=%s table=%s decision=%s reason=%s",
                    pdf_path,
                    complexity.get("page_count"),
                    complexity.get("pages_scanned"),
                    complexity.get("complex_pages"),
                    complexity.get("ocr_pages"),
                    complexity.get("table_like_pages"),
                    parser_mode,
                    complexity.get("reason"),
                )

            logging.info("PDF processing mode: %s + TOC/regex hierarchy", parser_mode)
            if parser_mode in {"pymupdf", "text"}:
                try:
                    extract_started = time.perf_counter()
                    pages = self._extract_pdf_pages_with_pymupdf(pdf_path)
                    logging.info(
                        "PyMuPDF text extraction done: source=%s pages=%s elapsed_ms=%s",
                        pdf_path,
                        len(pages),
                        int((time.perf_counter() - extract_started) * 1000),
                    )
                except Exception as e:
                    logging.warning("PyMuPDF page extraction unavailable, fallback to PyPDF2: %s", e)
                    extract_started = time.perf_counter()
                    pages = self._extract_pdf_pages_with_pypdf2(pdf_path)
                    logging.info(
                        "PyPDF2 text extraction done: source=%s pages=%s elapsed_ms=%s",
                        pdf_path,
                        len(pages),
                        int((time.perf_counter() - extract_started) * 1000),
                    )
                if self.pdf_table_mode in {"auto", "vision"}:
                    pages = self._enhance_pdf_table_pages_with_vision(pdf_path, pages)
            elif parser_mode == "ark_vision":
                pages = self._extract_pdf_pages_with_ark_vision(pdf_path)
            else:
                raise ValueError(f"Unsupported pdf_parser: {self.pdf_parser}")
            if not pages:
                raise RuntimeError("No pages extracted from PDF.")

            if parser_mode == "ark_vision":
                toc_entries = self._extract_pdf_toc_entries_with_pypdf2(pdf_path)
                if toc_entries:
                    logging.info("PDF TOC detected, entries=%s", len(toc_entries))
                    nodes = self._build_pdf_nodes_from_entries(toc_entries, pages)
                else:
                    logging.info("Ark vision PDF without native TOC, fallback to 2-page sliding window nodes")
                    nodes = self._build_pdf_sliding_window_nodes(pages, window_size=2, stride=1)
            else:
                toc_entries = self._extract_pdf_toc_entries_with_pypdf2(pdf_path)
                if toc_entries:
                    logging.info("PDF TOC detected, entries=%s", len(toc_entries))
                    nodes = self._build_pdf_nodes_from_entries(toc_entries, pages)
                else:
                    heading_entries = self._extract_pdf_headings_by_regex(pages)
                    if heading_entries:
                        logging.info("PDF TOC not found, regex headings detected, entries=%s", len(heading_entries))
                        nodes = self._build_pdf_nodes_from_entries(heading_entries, pages)
                    else:
                        logging.info("PDF TOC/regex headings not found, fallback to page-level nodes")
                        nodes = self._build_pdf_page_nodes(pages)

            if not nodes:
                nodes = self._build_pdf_page_nodes(pages)
            else:
                nodes = cap_pdf_tree_depth(nodes, self.pdf_max_depth)
                logging.info("PDF final tree depth cap applied: max_depth=%s", self.pdf_max_depth)

            structure: Dict[str, Any] = {
                "doc_type": "pdf",
                "doc_name": os.path.basename(pdf_path),
                "page_count": len(pages),
                "nodes": nodes,
                "pages": [{"page": num, "content": text} for num, text in pages if text],
            }
            if complexity is not None:
                structure["pdf_parse_decision"] = {
                    k: v for k, v in complexity.items() if k != "page_details"
                }
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logging.info(
                "PDF parse done: source=%s pages=%s nodes=%s elapsed_ms=%s",
                pdf_path,
                len(pages),
                len(nodes),
                elapsed_ms,
            )
            return self.enrich_with_metadata(structure, pdf_path, "pdf")
        except AppError as e:
            return self._error_result("pdf", pdf_path, e)
        except Exception as e:
            return self._error_result(
                "pdf",
                pdf_path,
                ParseError(f"PDF parse failed: {e}", stage="pdf_pypdf2_pipeline", cause=e),
            )

    def process_docx(self, docx_path: str) -> Dict[str, Any]:
        """Parse DOCX paragraphs/headings into hierarchical nodes."""
        try:
            try:
                logging.info("DOCX processing mode: LibreOffice DOCX->PDF + PDF parser")
                return self._process_docx_via_pdf(docx_path)
            except Exception as e:
                logging.warning("DOCX PDF pipeline unavailable, fallback to python-docx parser: %s", e)

            try:
                from docx import Document
            except ImportError as e:
                raise DependencyError("Missing dependency python-docx. Install: pip install python-docx", cause=e)

            doc = Document(docx_path)
            structure: Dict[str, Any] = {
                "doc_type": "docx",
                "doc_name": os.path.basename(docx_path),
                "paragraph_count": 0,
                "page_count": 0,
                "pages": [],
                "nodes": [],
            }

            node_id = 1
            paragraph_count = 0
            stack: List[Tuple[int, Dict[str, Any]]] = []
            all_paragraph_texts: List[str] = []

            def _attach_node(level: int, node: Dict[str, Any]) -> None:
                while stack and stack[-1][0] >= level:
                    stack.pop()
                if stack:
                    parent = stack[-1][1]
                    parent.setdefault("nodes", []).append(node)
                else:
                    structure["nodes"].append(node)
                stack.append((level, node))

            raw_non_empty_idx = 0
            for para in doc.paragraphs:
                text = clean_text(para.text)
                if not text:
                    continue

                raw_non_empty_idx += 1
                if raw_non_empty_idx <= 150 and looks_like_toc_entry(text):
                    continue

                paragraph_count += 1
                all_paragraph_texts.append(text)
                heading_level = detect_docx_heading_level(para, text)

                if heading_level is not None:
                    node = self._create_base_node(
                        node_id=node_id,
                        text=text,
                        page_number=None,
                        level=heading_level,
                        paragraph_index=paragraph_count,
                        start_index=paragraph_count,
                        end_index=paragraph_count,
                        title=text,
                        nodes=[],
                    )
                    if node is not None:
                        node["_body_chunks"] = []
                        _attach_node(heading_level, node)
                        node_id += 1
                    continue

                if stack:
                    current = stack[-1][1]
                    current.setdefault("_body_chunks", []).append(text)
                    current["end_index"] = paragraph_count
                else:
                    node = self._create_base_node(
                        node_id=node_id,
                        text=text,
                        page_number=None,
                        level=1,
                        paragraph_index=paragraph_count,
                        start_index=paragraph_count,
                        end_index=paragraph_count,
                        title=text[:30],
                    )
                    if node is not None:
                        structure["nodes"].append(node)
                        node_id += 1

            def _finalize_nodes(nodes: List[Dict[str, Any]]) -> None:
                """Merge collected body chunks and normalize heading ranges."""
                for node in nodes:
                    body_chunks = node.pop("_body_chunks", [])
                    body_text = clean_text("\n".join(body_chunks))
                    title = clean_text(node.get("title"))
                    if body_text:
                        node["text"] = f"{title}\n{body_text}" if title else body_text
                        node["_summary_source"] = node["text"]
                    elif title:
                        node["text"] = title
                    children = node.get("nodes")
                    if isinstance(children, list) and children:
                        _finalize_nodes(children)
                        child_starts = [c.get("start_index") for c in children if isinstance(c.get("start_index"), int)]
                        child_ends = [c.get("end_index") for c in children if isinstance(c.get("end_index"), int)]
                        if child_starts:
                            own_start = node.get("start_index")
                            if not isinstance(own_start, int):
                                node["start_index"] = min(child_starts)
                        if child_ends:
                            own_end = node.get("end_index")
                            if not isinstance(own_end, int):
                                node["end_index"] = max(child_ends)
                            else:
                                node["end_index"] = max([own_end] + child_ends)

            _finalize_nodes(structure["nodes"])
            structure["nodes"] = compact_docx_tree(structure["nodes"])

            structure["paragraph_count"] = paragraph_count
            paragraphs_per_page = 5
            if all_paragraph_texts:
                pages: List[Dict[str, Any]] = []
                for start in range(0, len(all_paragraph_texts), paragraphs_per_page):
                    chunk = all_paragraph_texts[start : start + paragraphs_per_page]
                    pages.append({"page": len(pages) + 1, "content": clean_text("\n".join(chunk))})
                structure["pages"] = pages
                structure["page_count"] = len(pages)
                docx_accuracy, docx_passed, docx_checked, docx_failures = verify_docx_title_page_alignment(
                    structure["nodes"], structure["pages"], paragraphs_per_page
                )
                logging.info(
                    "DOCX title-page alignment accuracy: %.2f%% (%s/%s)",
                    docx_accuracy * 100,
                    docx_passed,
                    docx_checked,
                )
                if docx_failures:
                    logging.warning("DOCX title-page alignment mismatches (showing up to 5): %s", docx_failures[:5])

            return self.enrich_with_metadata(structure, docx_path, "docx")
        except AppError as e:
            return self._error_result("docx", docx_path, e)
        except Exception as e:
            return self._error_result("docx", docx_path, ParseError(f"DOCX parse failed: {e}", stage="docx", cause=e))

    @staticmethod
    def _xlsx_escape_markdown_cell(value: Any) -> str:
        text = clean_text(value)
        return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    @staticmethod
    def _xlsx_contiguous_bands(indexes: List[int]) -> List[Tuple[int, int]]:
        if not indexes:
            return []
        bands: List[Tuple[int, int]] = []
        start = prev = indexes[0]
        for idx in indexes[1:]:
            if idx == prev + 1:
                prev = idx
                continue
            bands.append((start, prev))
            start = prev = idx
        bands.append((start, prev))
        return bands

    @staticmethod
    def _xlsx_range_label(row_start: int, row_end: int, col_start: int, col_end: int) -> str:
        try:
            from openpyxl.utils import get_column_letter

            return f"{get_column_letter(col_start)}{row_start}:{get_column_letter(col_end)}{row_end}"
        except Exception:
            return f"R{row_start}C{col_start}:R{row_end}C{col_end}"

    def _xlsx_sheet_to_filled_grid(self, ws: Any, max_rows: int) -> Tuple[List[List[str]], Set[Tuple[int, int]], bool]:
        """Read a worksheet into a grid and fill merged cells for region detection.

        The returned merged_shadow_cells marks non-top-left cells inside merged ranges.
        Those cells are blanked only when rendering Markdown, avoiding repeated values
        while still letting detection understand the merged range's footprint.
        """
        max_row = int(getattr(ws, "max_row", 0) or 0)
        max_col = int(getattr(ws, "max_column", 0) or 0)
        truncated = False
        if max_rows and max_row > max_rows:
            max_row = max_rows
            truncated = True

        grid: List[List[str]] = [["" for _ in range(max_col)] for _ in range(max_row)]
        for r_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=max_row, max_col=max_col, values_only=True), 1):
            for c_idx, value in enumerate(row, 1):
                grid[r_idx - 1][c_idx - 1] = clean_text(value)

        merged_shadow_cells: Set[Tuple[int, int]] = set()
        merged_ranges = getattr(getattr(ws, "merged_cells", None), "ranges", []) or []
        for merged_range in merged_ranges:
            min_row = int(getattr(merged_range, "min_row", 0) or 0)
            max_m_row = int(getattr(merged_range, "max_row", 0) or 0)
            min_col = int(getattr(merged_range, "min_col", 0) or 0)
            max_m_col = int(getattr(merged_range, "max_col", 0) or 0)
            if min_row < 1 or min_col < 1 or min_row > max_row:
                continue
            value = grid[min_row - 1][min_col - 1] if min_col <= max_col else ""
            if not value:
                continue
            for row_idx in range(min_row, min(max_m_row, max_row) + 1):
                for col_idx in range(min_col, min(max_m_col, max_col) + 1):
                    grid[row_idx - 1][col_idx - 1] = value
                    if (row_idx, col_idx) != (min_row, min_col):
                        merged_shadow_cells.add((row_idx, col_idx))

        return grid, merged_shadow_cells, truncated

    def _detect_xlsx_table_regions(self, grid: List[List[str]]) -> List[Tuple[int, int, int, int]]:
        """Detect non-empty rectangular regions separated by blank rows/columns."""
        if not grid:
            return []

        row_indexes = [
            row_idx
            for row_idx, row in enumerate(grid, 1)
            if any(clean_text(cell) for cell in row)
        ]
        regions: List[Tuple[int, int, int, int]] = []
        for row_start, row_end in self._xlsx_contiguous_bands(row_indexes):
            col_indexes: List[int] = []
            max_col = max((len(row) for row in grid[row_start - 1 : row_end]), default=0)
            for col_idx in range(1, max_col + 1):
                if any(
                    col_idx <= len(grid[row_idx - 1]) and clean_text(grid[row_idx - 1][col_idx - 1])
                    for row_idx in range(row_start, row_end + 1)
                ):
                    col_indexes.append(col_idx)

            for col_start, col_end in self._xlsx_contiguous_bands(col_indexes):
                occupied: List[Tuple[int, int]] = []
                for row_idx in range(row_start, row_end + 1):
                    row = grid[row_idx - 1]
                    for col_idx in range(col_start, min(col_end, len(row)) + 1):
                        if clean_text(row[col_idx - 1]):
                            occupied.append((row_idx, col_idx))

                if not occupied:
                    continue
                trimmed_row_start = min(item[0] for item in occupied)
                trimmed_row_end = max(item[0] for item in occupied)
                trimmed_col_start = min(item[1] for item in occupied)
                trimmed_col_end = max(item[1] for item in occupied)
                regions.append((trimmed_row_start, trimmed_row_end, trimmed_col_start, trimmed_col_end))

        return regions

    def _xlsx_region_to_markdown(
        self,
        grid: List[List[str]],
        row_start: int,
        row_end: int,
        col_start: int,
        col_end: int,
        merged_shadow_cells: Optional[Set[Tuple[int, int]]] = None,
    ) -> str:
        merged_shadow_cells = merged_shadow_cells or set()
        rows: List[List[str]] = []
        for row_idx in range(row_start, row_end + 1):
            source_row = grid[row_idx - 1] if row_idx - 1 < len(grid) else []
            row_values = [
                self._xlsx_escape_markdown_cell(
                    "" if (row_idx, col_idx) in merged_shadow_cells
                    else (source_row[col_idx - 1] if col_idx - 1 < len(source_row) else "")
                )
                for col_idx in range(col_start, col_end + 1)
            ]
            rows.append(row_values)

        while rows and not any(rows[0]):
            rows.pop(0)
        while rows and not any(rows[-1]):
            rows.pop()
        if not rows:
            return ""

        max_cols = max(len(row) for row in rows)
        normalized_rows = [row + [""] * (max_cols - len(row)) for row in rows]
        header = normalized_rows[0]
        if not any(header):
            header = [f"Column {idx}" for idx in range(1, max_cols + 1)]

        markdown_rows = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(["---"] * max_cols) + " |",
        ]
        markdown_rows.extend("| " + " | ".join(row) + " |" for row in normalized_rows[1:])
        return "\n".join(markdown_rows)

    @staticmethod
    def _xlsx_region_title(sheet_name: str, range_label: str, region_index: int) -> str:
        return f"{sheet_name} 表格{region_index} ({range_label})"

    def process_xlsx(self, xlsx_path: str) -> Dict[str, Any]:
        """Parse XLSX sheets into table-region nodes."""
        wb = None
        try:
            try:
                import openpyxl
            except ImportError as e:
                raise DependencyError("Missing dependency openpyxl. Install: pip install openpyxl", cause=e)

            wb = openpyxl.load_workbook(xlsx_path, read_only=False, data_only=True)
            structure: Dict[str, Any] = {
                "doc_type": "xlsx",
                "doc_name": os.path.basename(xlsx_path),
                "sheet_count": len(wb.sheetnames),
                "nodes": [],
                "pages": [],
            }

            node_id = 1
            for sheet_index, sheet_name in enumerate(wb.sheetnames, 1):
                sheet_started_at = time.perf_counter()
                ws = wb[sheet_name]
                max_rows = self.max_xlsx_rows_per_sheet or int(getattr(ws, "max_row", 0) or 0)
                grid, merged_shadow_cells, truncated = self._xlsx_sheet_to_filled_grid(ws, max_rows=max_rows)
                regions = self._detect_xlsx_table_regions(grid)
                if not regions:
                    logging.info("Skip empty sheet: %s", sheet_name)
                    continue

                logging.info(
                    "XLSX sheet table regions detected: sheet=%s regions=%s truncated=%s elapsed_ms=%s",
                    sheet_name,
                    len(regions),
                    truncated,
                    int((time.perf_counter() - sheet_started_at) * 1000),
                )

                for region_index, (row_start, row_end, col_start, col_end) in enumerate(regions, 1):
                    table_page = node_id
                    range_label = self._xlsx_range_label(row_start, row_end, col_start, col_end)
                    markdown = self._xlsx_region_to_markdown(
                        grid,
                        row_start,
                        row_end,
                        col_start,
                        col_end,
                        merged_shadow_cells=merged_shadow_cells,
                    )
                    if not markdown:
                        continue

                    row_count = row_end - row_start + 1
                    col_count = col_end - col_start + 1
                    title = self._xlsx_region_title(sheet_name, range_label, region_index)
                    table_text = clean_text(
                        f"Sheet: {sheet_name}\n"
                        f"Range: {range_label}\n"
                        f"Table: {title}\n\n"
                        f"{markdown}"
                    )

                    node = self._create_base_node(
                        node_id=node_id,
                        text=table_text,
                        page_number=None,
                        level=1,
                        title=title,
                        start_index=table_page,
                        end_index=table_page,
                        sheet_index=sheet_index,
                        sheet_name=sheet_name,
                        table_index=region_index,
                        range=range_label,
                        row_start=row_start,
                        row_end=row_end,
                        col_start=col_start,
                        col_end=col_end,
                        row_count=row_count,
                        col_count=col_count,
                        truncated=truncated,
                    )
                    if node is not None:
                        node["_summary_source"] = table_text[: max(self.summary_input_chars * 2, 8000)]
                        structure["nodes"].append(node)
                        structure["pages"].append(
                            {
                                "page": table_page,
                                "sheet_index": sheet_index,
                                "sheet_name": sheet_name,
                                "table_index": region_index,
                                "range": range_label,
                                "content": table_text,
                            }
                        )
                        logging.info(
                            "XLSX table region parsed: sheet=%s table=%s range=%s rows=%s cols=%s chars=%s",
                            sheet_name,
                            region_index,
                            range_label,
                            row_count,
                            col_count,
                            len(table_text),
                        )
                        node_id += 1

            return self.enrich_with_metadata(structure, xlsx_path, "xlsx")
        except AppError as e:
            return self._error_result("xlsx", xlsx_path, e)
        except Exception as e:
            return self._error_result("xlsx", xlsx_path, ParseError(f"XLSX parse failed: {e}", stage="xlsx", cause=e))
        finally:
            if wb is not None:
                wb.close()

    def process_txt(self, txt_path: str) -> Dict[str, Any]:
        """Parse TXT into paragraph/title-aware chunks aligned with pages."""
        try:
            structure: Dict[str, Any] = {
                "doc_type": "txt",
                "doc_name": os.path.basename(txt_path),
                "line_count": 0,
                "nodes": [],
                "pages": [],
            }

            encoding = detect_text_encoding(txt_path)
            logging.info("TXT detected encoding: %s", encoding)

            paragraphs: List[Dict[str, Any]] = []
            paragraph_lines: List[str] = []
            paragraph_start: Optional[int] = None
            paragraph_end: Optional[int] = None
            raw_line_count = 0
            non_empty_line_count = 0

            def _flush_paragraph() -> None:
                nonlocal paragraph_lines, paragraph_start, paragraph_end
                if not paragraph_lines or paragraph_start is None or paragraph_end is None:
                    paragraph_lines = []
                    paragraph_start = None
                    paragraph_end = None
                    return
                text = clean_text("\n".join(paragraph_lines))
                if text:
                    paragraphs.append(
                        {
                            "text": text,
                            "line_start": paragraph_start,
                            "line_end": paragraph_end,
                        }
                    )
                paragraph_lines = []
                paragraph_start = None
                paragraph_end = None

            with open(txt_path, "r", encoding=encoding, errors="replace") as f:
                for raw_line_index, raw_line in enumerate(f, 1):
                    raw_line_count = raw_line_index
                    text = clean_text(raw_line)
                    if not text:
                        _flush_paragraph()
                        continue

                    non_empty_line_count += 1
                    if paragraph_start is None:
                        paragraph_start = raw_line_index
                    paragraph_end = raw_line_index
                    paragraph_lines.append(text)

            _flush_paragraph()

            def _txt_heading_level(text: str) -> Optional[int]:
                sample = clean_text(text.splitlines()[0] if text else "")
                if not sample or len(sample) > 80:
                    return None
                if re.match(r"^#{1,6}\s+\S+", sample):
                    return min(6, len(sample) - len(sample.lstrip("#")))
                if re.match(r"^第[一二三四五六七八九十百千万0-9]+[章节篇部]\s*\S*", sample):
                    return 1
                if re.match(r"^[一二三四五六七八九十]+[、.．]\s*\S+", sample):
                    return 1
                if re.match(r"^（[一二三四五六七八九十]+）\s*\S+", sample):
                    return 2
                m = re.match(r"^(\d+(?:\.\d+)*)(?:[、.．]|\s+)\S+", sample)
                if m:
                    return 1 + m.group(1).count(".")
                return None

            node_id = 1
            flat_nodes: List[Dict[str, Any]] = []
            current_chunk: List[str] = []
            chunk_start: Optional[int] = None
            chunk_last_line: Optional[int] = None
            chunk_title = ""
            chunk_level = 1
            max_chunk_chars = self.txt_chars_per_node

            def _emit_chunk() -> None:
                nonlocal node_id, current_chunk, chunk_start, chunk_last_line, chunk_title, chunk_level
                if not current_chunk or chunk_start is None or chunk_last_line is None:
                    current_chunk = []
                    chunk_start = None
                    chunk_last_line = None
                    chunk_title = ""
                    chunk_level = 1
                    return
                page_num = node_id
                chunk_text = clean_text("\n\n".join(current_chunk))
                if not chunk_text:
                    return
                node = self._create_base_node(
                    node_id=node_id,
                    text=chunk_text,
                    page_number=None,
                    level=chunk_level,
                    title=chunk_title or f"文本片段 {page_num}",
                    start_index=page_num,
                    end_index=page_num,
                    line_start=chunk_start,
                    line_end=chunk_last_line,
                )
                if node is not None:
                    node["_summary_source"] = chunk_text[: max(self.summary_input_chars * 2, 8000)]
                    flat_nodes.append(node)
                    structure["pages"].append(
                        {
                            "page": page_num,
                            "line_start": chunk_start,
                            "line_end": chunk_last_line,
                            "content": chunk_text,
                        }
                    )
                    logging.info(
                        "TXT chunk parsed: page=%s title=%s lines=%s-%s chars=%s",
                        page_num,
                        chunk_title or f"文本片段 {page_num}",
                        chunk_start,
                        chunk_last_line,
                        len(chunk_text),
                    )
                    node_id += 1
                current_chunk = []
                chunk_start = None
                chunk_last_line = None
                chunk_title = ""
                chunk_level = 1

            for paragraph in paragraphs:
                text = paragraph["text"]
                line_start = int(paragraph["line_start"])
                line_end = int(paragraph["line_end"])
                heading_level = _txt_heading_level(text)

                if heading_level is not None and current_chunk:
                    _emit_chunk()

                candidate_chars = len("\n\n".join(current_chunk)) + len(text)
                if current_chunk and candidate_chars > max_chunk_chars:
                    _emit_chunk()

                if not current_chunk:
                    chunk_start = line_start
                    chunk_title = clean_text(text.splitlines()[0])[:80]
                    chunk_level = heading_level or 1

                current_chunk.append(text)
                chunk_last_line = line_end

            _emit_chunk()

            def _build_txt_tree(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
                roots: List[Dict[str, Any]] = []
                stack: List[Dict[str, Any]] = []

                for node in nodes:
                    level = int(node.get("level") or 1)
                    while stack and int(stack[-1].get("level") or 1) >= level:
                        stack.pop()
                    if stack:
                        stack[-1].setdefault("nodes", []).append(node)
                    else:
                        roots.append(node)
                    stack.append(node)

                def _refresh_range(items: List[Dict[str, Any]]) -> None:
                    for item in items:
                        children = item.get("nodes")
                        if isinstance(children, list) and children:
                            _refresh_range(children)
                            child_end_indexes = [
                                child.get("end_index")
                                for child in children
                                if isinstance(child.get("end_index"), int)
                            ]
                            child_line_ends = [
                                child.get("line_end")
                                for child in children
                                if isinstance(child.get("line_end"), int)
                            ]
                            if child_end_indexes:
                                item["end_index"] = max(
                                    int(item.get("end_index") or item.get("start_index") or 1),
                                    max(child_end_indexes),
                                )
                            if child_line_ends:
                                item["line_end"] = max(
                                    int(item.get("line_end") or item.get("line_start") or 1),
                                    max(child_line_ends),
                                )

                _refresh_range(roots)
                return roots

            structure["nodes"] = _build_txt_tree(flat_nodes)
            structure["line_count"] = non_empty_line_count
            structure["raw_line_count"] = raw_line_count
            structure["page_count"] = len(structure["pages"])
            return self.enrich_with_metadata(structure, txt_path, "txt")
        except AppError as e:
            return self._error_result("txt", txt_path, e)
        except Exception as e:
            return self._error_result("txt", txt_path, ParseError(f"TXT parse failed: {e}", stage="txt", cause=e))

    @staticmethod
    def _pptx_shape_position(shape: Any) -> Tuple[int, int]:
        """Sort PPTX shapes by visual reading order: top-to-bottom, then left-to-right."""
        top = getattr(shape, "top", 0) or 0
        left = getattr(shape, "left", 0) or 0
        try:
            return int(top), int(left)
        except Exception:
            return 0, 0

    def _iter_pptx_shapes(self, shapes: Any) -> List[Any]:
        """Flatten slide/group shapes while preserving text and table shapes."""
        flattened: List[Any] = []
        for shape in shapes:
            group_shapes = getattr(shape, "shapes", None)
            if group_shapes is not None:
                flattened.extend(self._iter_pptx_shapes(group_shapes))
            else:
                flattened.append(shape)
        return flattened

    @staticmethod
    def _pptx_cell_text(cell: Any) -> str:
        text = clean_text(getattr(cell, "text", ""))
        return text.replace("|", "\\|")

    def _pptx_table_to_markdown(self, table: Any) -> str:
        """Convert a python-pptx table object to Markdown."""
        rows: List[List[str]] = []
        for row in getattr(table, "rows", []):
            cells = [self._pptx_cell_text(cell) for cell in getattr(row, "cells", [])]
            if any(cells):
                rows.append(cells)
        if not rows:
            return ""

        max_cols = max(len(row) for row in rows)
        normalized_rows = [row + [""] * (max_cols - len(row)) for row in rows]
        markdown_rows = [
            "| " + " | ".join(normalized_rows[0]) + " |",
            "| " + " | ".join(["---"] * max_cols) + " |",
        ]
        markdown_rows.extend("| " + " | ".join(row) + " |" for row in normalized_rows[1:])
        return "[表格]\n" + "\n".join(markdown_rows)

    @staticmethod
    def _extract_pptx_slide_title(slide: Any) -> str:
        """Prefer explicit title placeholder; fallback to the topmost short text shape."""
        try:
            title_shape = getattr(slide.shapes, "title", None)
            title = clean_text(getattr(title_shape, "text", "")) if title_shape is not None else ""
            if title:
                return title
        except Exception:
            pass

        candidates: List[Tuple[Tuple[int, int], str]] = []
        for shape in slide.shapes:
            if not hasattr(shape, "text"):
                continue
            text = clean_text(getattr(shape, "text", ""))
            if not text:
                continue
            first_line = clean_text(text.splitlines()[0])
            if first_line and len(first_line) <= 80:
                top = getattr(shape, "top", 0) or 0
                left = getattr(shape, "left", 0) or 0
                candidates.append(((int(top), int(left)), first_line))
        if candidates:
            candidates.sort(key=lambda item: item[0])
            return candidates[0][1]
        return ""

    @staticmethod
    def _extract_pptx_notes_text(slide: Any) -> str:
        """Extract speaker notes when present."""
        try:
            notes_slide = getattr(slide, "notes_slide", None)
            notes_text_frame = getattr(notes_slide, "notes_text_frame", None)
            return clean_text(getattr(notes_text_frame, "text", ""))
        except Exception:
            return ""

    def _extract_pptx_slide_local_text(self, slide: Any, slide_num: int, title: str) -> str:
        """Extract slide text/tables with python-pptx."""
        title_shape_id = None
        try:
            title_shape = getattr(slide.shapes, "title", None)
            title_shape_id = getattr(title_shape, "shape_id", None) if title_shape is not None else None
        except Exception:
            title_shape_id = None

        texts: List[str] = [f"# {title}"]
        for shape in sorted(self._iter_pptx_shapes(slide.shapes), key=self._pptx_shape_position):
            if getattr(shape, "shape_id", None) == title_shape_id:
                continue

            if getattr(shape, "has_table", False):
                table_text = self._pptx_table_to_markdown(shape.table)
                if table_text:
                    texts.append(table_text)
                continue

            if hasattr(shape, "text"):
                text = clean_text(getattr(shape, "text", ""))
                if text and text != title:
                    texts.append(text)

        notes_text = self._extract_pptx_notes_text(slide)
        if notes_text:
            texts.append(f"[备注]\n{notes_text}")

        return clean_text("\n".join(texts))

    def process_pptx(self, pptx_path: str) -> Dict[str, Any]:
        """Parse PPTX slides into slide-level text nodes."""
        try:
            try:
                from pptx import Presentation
            except ImportError as e:
                raise DependencyError("Missing dependency python-pptx. Install: pip install python-pptx", cause=e)

            prs = Presentation(pptx_path)
            structure: Dict[str, Any] = {
                "doc_type": "pptx",
                "doc_name": os.path.basename(pptx_path),
                "slide_count": len(prs.slides),
                "page_count": len(prs.slides),
                "nodes": [],
                "pages": [],
            }

            node_id = 1
            for slide_num, slide in enumerate(prs.slides, 1):
                slide_started_at = time.perf_counter()
                title = self._extract_pptx_slide_title(slide) or f"Slide {slide_num}"
                combined = self._extract_pptx_slide_local_text(slide, slide_num, title)
                if not combined:
                    continue

                logging.info(
                    "PPTX slide parsed: slide=%s mode=local chars=%s elapsed_ms=%s",
                    slide_num,
                    len(combined),
                    int((time.perf_counter() - slide_started_at) * 1000),
                )

                node = self._create_base_node(
                    node_id=node_id,
                    text=combined,
                    page_number=None,
                    level=1,
                    slide_number=slide_num,
                    title=title,
                    start_index=slide_num,
                    end_index=slide_num,
                )
                if node is not None:
                    node["_summary_source"] = combined[: max(self.summary_input_chars * 2, 8000)]
                    structure["nodes"].append(node)
                    structure["pages"].append({"page": slide_num, "content": combined})
                    node_id += 1

            return self.enrich_with_metadata(structure, pptx_path, "pptx")
        except AppError as e:
            return self._error_result("pptx", pptx_path, e)
        except Exception as e:
            return self._error_result("pptx", pptx_path, ParseError(f"PPTX parse failed: {e}", stage="pptx", cause=e))


