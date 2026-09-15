# 模块说明：批量问答执行、断点续跑与结果落盘。
import json
import logging
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import ValidationError

from core.llm_ops import DEFAULT_NO_INFO_ANSWER
from models.schemas import AnswerRow, QuestionItem
from utils.runtime import get_client_runtime

logger = logging.getLogger(__name__)


def extract_questions_from_docx(docx_path: Path) -> list[dict]:
    """从 DOCX 中提取题目列表。

    兼容多种题号格式（阿拉伯数字、中文数字、圈号等），
    最终返回标准化的问题对象列表。
    """
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(docx_path) as zf:
        xml_bytes = zf.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    questions = []
    fallback_paragraphs = []
    circled_numbers = {
        "①": 1, "②": 2, "③": 3, "④": 4, "⑤": 5, "⑥": 6, "⑦": 7, "⑧": 8, "⑨": 9, "⑩": 10,
        "⑪": 11, "⑫": 12, "⑬": 13, "⑭": 14, "⑮": 15, "⑯": 16, "⑰": 17, "⑱": 18, "⑲": 19, "⑳": 20,
    }

    def chinese_num_to_int(text: str) -> int | None:
        """将中文数字（如“十二”）转换为整数。"""
        if not text:
            return None
        if text.isdigit():
            return int(text)
        mapping = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        unit = {"十": 10, "百": 100}
        total = 0
        current = 0
        for ch in text:
            if ch in mapping:
                current = mapping[ch]
            elif ch in unit:
                if current == 0:
                    current = 1
                total += current * unit[ch]
                current = 0
            else:
                return None
        total += current
        return total if total > 0 else None

    def parse_question_line(paragraph: str) -> tuple[int | None, str | None]:
        """解析单段文本，尝试提取题号与题干。"""
        text = paragraph.strip()
        if not text:
            return None, None
        patterns = [
            r"^\s*(\d+)[\.\u3001\uFF0E\)]\s*(.+?)\s*$",
            r"^\s*(\d+)\s*[：:]\s*(.+?)\s*$",
            r"^\s*[\(（](\d+)[\)）]\s*(.+?)\s*$",
            r"^\s*[\(（]([一二三四五六七八九十百零\d]+)[\)）]\s*(.+?)\s*$",
            r"^\s*第([一二三四五六七八九十百零\d]+)\s*[题問]\s*[：:\.\u3001]?\s*(.+?)\s*$",
            r"^\s*[问問][题題]\s*([一二三四五六七八九十百零\d]+)\s*[：:\.\u3001\)]?\s*(.+?)\s*$",
        ]
        for pattern in patterns:
            match = re.match(pattern, text)
            if not match:
                continue
            raw_id = match.group(1)
            qid = int(raw_id) if raw_id.isdigit() else chinese_num_to_int(raw_id)
            body = match.group(2).strip()
            if qid is not None and body:
                return qid, body
        if text and text[0] in circled_numbers:
            qid = circled_numbers[text[0]]
            body = text[1:].strip(" .、:：）)")
            if body:
                return qid, body
        return None, None

    for para in root.findall(".//w:body/w:p", namespace):
        texts = [node.text for node in para.findall(".//w:t", namespace) if node.text]
        paragraph_text = "".join(texts).strip()
        if not paragraph_text:
            continue
        qid, question_text = parse_question_line(paragraph_text)
        if qid is not None and question_text:
            try:
                questions.append(QuestionItem(id=qid, question=question_text, raw_text=paragraph_text).model_dump())
            except ValidationError as exc:
                logger.warning("Skip invalid question row '%s': %s", paragraph_text, exc)
        else:
            fallback_paragraphs.append(paragraph_text)

    if not questions:
        logger.warning("No numbered questions found, fallback to non-empty paragraph extraction: %s", docx_path)
        for idx, paragraph in enumerate(fallback_paragraphs, 1):
            if len(paragraph) < 4:
                continue
            try:
                questions.append(QuestionItem(id=idx, question=paragraph, raw_text=paragraph).model_dump())
            except ValidationError:
                continue
    if not questions:
        raise ValueError(f"No numbered questions found in DOCX: {docx_path}")
    return questions


def load_expected_docs(expected_docs_json: Path | None) -> dict[int, list[str]]:
    """加载期望文档映射（用于评测或对齐分析）。"""
    if expected_docs_json is None or not expected_docs_json.exists():
        return {}
    with open(expected_docs_json, "r", encoding="utf-8") as f:
        payload = json.load(f)
    mapping = {}
    if isinstance(payload, list):
        for item in payload:
            question_id = item.get("id") or item.get("index")
            doc_ids = item.get("doc_ids") or item.get("expected_doc_ids") or []
            if question_id is not None:
                mapping[int(question_id)] = list(doc_ids)
    elif isinstance(payload, dict):
        for key, value in payload.items():
            try:
                mapping[int(key)] = list(value)
            except (TypeError, ValueError):
                continue
    return mapping


async def answer_questions_from_docx(
    client,
    *,
    execute_qa_fn,
    questions_docx: Path,
    output_json: Path,
    verbose: bool,
    qa_mode: str,
    top_k_docs: int,
    resume: bool,
    expected_docs_json: Path | None = None,
    allowed_doc_ids: set[str] | None = None,
    bm25_prefilter=None,
    bm25_prefilter_top_k: int = 8,
    session_id: str | None = None,
):
    """批量执行 DOCX 题目问答并持续写出结果。

    支持断点续跑（resume）、错误记录、增量落盘，
    适合长批次任务在中断后恢复执行。
    """
    questions = extract_questions_from_docx(questions_docx)
    expected_docs = load_expected_docs(expected_docs_json)
    if expected_docs and qa_mode == "agent":
        logger.warning("--expected-docs-json is ignored in Agent mode because selected doc IDs are not exposed.")
    logger.info("Loaded %s questions from: %s", len(questions), questions_docx)
    started_at = datetime.now()
    start_perf = time.perf_counter()
    output_json.parent.mkdir(parents=True, exist_ok=True)
    results = []
    errors = []
    processed_ids: set[int] = set()
    if resume and output_json.exists():
        try:
            with open(output_json, "r", encoding="utf-8") as f:
                old_payload = json.load(f)
            old_results = old_payload.get("results", [])
            if isinstance(old_results, list):
                results = old_results
                processed_ids = {int(item.get("id")) for item in old_results if isinstance(item, dict) and item.get("id") is not None}
            old_errors = old_payload.get("errors", [])
            if isinstance(old_errors, list):
                errors = old_errors
            logger.info("Resume enabled. Loaded %s completed results from %s", len(processed_ids), output_json)
        except Exception as exc:
            logger.warning("Failed to load resume file %s: %s", output_json, exc)
            results = []
            errors = []
            processed_ids = set()

    def write_progress(final: bool = False):
        """将当前进度写入输出文件。"""
        now = datetime.now()
        payload = {
            "source_document": str(questions_docx),
            "qa_mode": qa_mode,
            "total_questions": len(questions),
            "completed_questions": len(results),
            "started_at": started_at.isoformat(timespec="seconds"),
            "ended_at": now.isoformat(timespec="seconds") if final else None,
            "elapsed_seconds": round(time.perf_counter() - start_perf, 2),
            "resume_enabled": bool(resume),
            "results": results,
            "errors": errors,
        }
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    for item in questions:
        question_id = item["id"]
        question = item["question"]
        if question_id in processed_ids:
            logger.info("Skip already completed question %s (resume)", question_id)
            continue
        logger.info("Question %s: %s", question_id, question)
        try:
            effective_session_id = (
                f"{session_id}-q{question_id}" if session_id else f"batch-q{question_id}"
            )
            qa_result = await execute_qa_fn(
                client,
                question,
                qa_mode=qa_mode,
                verbose=verbose,
                stream_output=False,
                top_k_docs=top_k_docs,
                allowed_doc_ids=allowed_doc_ids,
                bm25_prefilter=bm25_prefilter,
                bm25_prefilter_top_k=bm25_prefilter_top_k,
                session_id=effective_session_id,
            )
            answer = qa_result["answer"]
            selected_documents = qa_result.get("selected_documents", [])
            evidence = qa_result.get("evidence", [])
        except Exception as exc:
            runtime = get_client_runtime(client)
            runtime.stats["qa_failures"] = int(runtime.stats.get("qa_failures", 0)) + 1
            logger.exception("Question %s failed", question_id)
            answer = DEFAULT_NO_INFO_ANSWER
            selected_documents = []
            evidence = []
            errors.append({"id": question_id, "question": question, "error": str(exc), "timestamp": datetime.now().isoformat(timespec="seconds")})
        row = {
            "id": question_id,
            "question": question,
            "answer": answer if answer else DEFAULT_NO_INFO_ANSWER,
            "selected_documents": selected_documents,
            "evidence": evidence,
        }
        try:
            row = AnswerRow.model_validate(row).model_dump()
        except ValidationError as exc:
            logger.error("Invalid answer row for question_id=%s: %s", question_id, exc)
        if verbose:
            logger.debug("selected_documents=%s", json.dumps(row["selected_documents"], ensure_ascii=False))
            logger.debug("evidence=%s", json.dumps(row["evidence"], ensure_ascii=False))
        logger.info("answer=%s", row["answer"])
        results.append(row)
        processed_ids.add(question_id)
        write_progress(final=False)

    write_progress(final=True)
    logger.info("Saved answers to: %s", output_json)
    logger.info("Completed %s/%s questions", len(results), len(questions))
    logger.info("Error count: %s", len(errors))
    if expected_docs and qa_mode == "agent":
        logger.info("Document selection accuracy skipped in Agent batch mode.")

