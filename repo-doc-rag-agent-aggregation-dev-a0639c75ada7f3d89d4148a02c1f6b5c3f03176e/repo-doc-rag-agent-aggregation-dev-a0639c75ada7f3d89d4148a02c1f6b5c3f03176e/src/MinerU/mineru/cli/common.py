# Copyright (c) Opendatalab. All rights reserved.
import asyncio
import csv
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO, StringIO
from pathlib import Path
from typing import Sequence

from loguru import logger

from mineru.cli.backend_options import (
    DEFAULT_HYBRID_EFFORT,
    normalize_backend,
    validate_effort,
)
from mineru.cli.output_paths import build_parse_dir
from mineru.data.data_reader_writer import FileBasedDataWriter
from mineru.utils.draw_bbox import draw_layout_bbox, draw_span_bbox
from mineru.utils.engine_utils import get_vlm_engine
from mineru.utils.enum_class import MakeMode
from mineru.utils.guess_suffix_or_lang import guess_suffix_by_bytes
from mineru.utils.pdf_image_tools import images_bytes_to_pdf_bytes
from mineru.backend.vlm.vlm_middle_json_mkcontent import union_make as vlm_union_make
from mineru.backend.office.office_middle_json_mkcontent import union_make as office_union_make
from mineru.backend.html.html_analyze import html_url_analyze
from mineru.backend.html.html_middle_json_mkcontent import union_make as html_union_make
from mineru.backend.text.text_analyze import text_doc_analyze
from mineru.backend.vlm.vlm_analyze import doc_analyze as vlm_doc_analyze
from mineru.backend.vlm.vlm_analyze import aio_doc_analyze as aio_vlm_doc_analyze
from mineru.backend.office.pptx_analyze import office_pptx_analyze
from mineru.backend.office.xlsx_analyze import office_xlsx_analyze
from mineru.backend.office.docx_analyze import office_docx_analyze
from mineru.utils.pdfium_guard import (
    get_loadable_pdfium_page_indices,
    rewrite_pdf_bytes_with_pdfium,
)

os.environ["TORCH_CUDNN_V8_API_DISABLED"] = "1"
if os.getenv("MINERU_LMDEPLOY_DEVICE", "") == "maca":
    import torch
    torch.backends.cudnn.enabled = False


pdf_suffixes = ["pdf"]
image_suffixes = ["png", "jpeg", "jp2", "webp", "gif", "bmp", "jpg", "tiff"]
plain_text_suffixes = ["txt"]
markdown_suffixes = ["md", "markdown"]
text_input_suffixes = plain_text_suffixes + markdown_suffixes
html_input_suffixes = ["html"]
doc_suffixes = ["doc"]
docx_suffixes = ["docx"]
ppt_suffixes = ["ppt"]
pptx_suffixes = ["pptx"]
xls_suffixes = ["xls"]
xlsx_suffixes = ["xlsx"]
csv_suffixes = ["csv"]
spreadsheet_input_suffixes = xls_suffixes + xlsx_suffixes + csv_suffixes
office_suffixes = docx_suffixes + pptx_suffixes + xlsx_suffixes
office_input_suffixes = (
    doc_suffixes
    + docx_suffixes
    + ppt_suffixes
    + pptx_suffixes
    + spreadsheet_input_suffixes
)
OFFICE_CONVERTER_BINARY_ENV = "MINERU_OFFICE_CONVERTER"
DOC_CONVERTER_BINARY_ENV = "MINERU_DOC_CONVERTER"
PRESENTATION_CONVERTER_BINARY_ENV = "MINERU_PRESENTATION_CONVERTER"
SPREADSHEET_CONVERTER_BINARY_ENV = "MINERU_SPREADSHEET_CONVERTER"

os.environ["TOKENIZERS_PARALLELISM"] = "false"
# Maximum UTF-8 byte length allowed for task stems used in filenames.
# 200 bytes is chosen to stay well below common filesystem limits (e.g. 255 bytes)
# and to prevent generating excessively long or incompatible filenames.
MAX_TASK_STEM_BYTES = 200


class HybridDependencyError(RuntimeError):
    pass


class LegacyWordConversionError(RuntimeError):
    pass


class LegacyPresentationConversionError(RuntimeError):
    pass


class LegacySpreadsheetConversionError(RuntimeError):
    pass


def _resolve_office_converter_binary(specific_env_var: str, input_description: str) -> str:
    candidates = []
    for env_var in (specific_env_var, OFFICE_CONVERTER_BINARY_ENV):
        configured_binary = os.getenv(env_var, "").strip()
        if configured_binary:
            candidates.append(configured_binary)
    candidates.extend(["soffice", "libreoffice"])
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise RuntimeError(
        f"{input_description} requires LibreOffice/soffice for conversion. "
        f"Install LibreOffice or set {specific_env_var} / {OFFICE_CONVERTER_BINARY_ENV} "
        "to the converter binary."
    )


def _convert_bytes_with_libreoffice(
    file_bytes: bytes,
    source_name: str,
    input_suffix: str,
    output_suffix: str,
    converter_env_var: str,
    timeout_env_var: str,
    input_description: str,
) -> bytes:
    try:
        converter_binary = _resolve_office_converter_binary(
            converter_env_var,
            input_description,
        )
    except RuntimeError as exc:
        raise RuntimeError(str(exc)) from exc

    with tempfile.TemporaryDirectory(prefix=f"mineru-{input_suffix}-convert-") as temp_dir:
        temp_path = Path(temp_dir)
        input_path = temp_path / Path(source_name).name
        if input_path.suffix.lower() != f".{input_suffix}":
            input_path = input_path.with_suffix(f".{input_suffix}")
        output_dir = temp_path / "out"
        profile_dir = temp_path / "lo-profile"
        output_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.mkdir(parents=True, exist_ok=True)
        input_path.write_bytes(file_bytes)

        command = [
            converter_binary,
            "--headless",
            "--nologo",
            "--nofirststartwizard",
            "--nolockcheck",
            "--nodefault",
            "--norestore",
            f"-env:UserInstallation=file://{profile_dir.as_posix()}",
            "--convert-to",
            output_suffix,
            "--outdir",
            str(output_dir),
            str(input_path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=int(
                    os.getenv(
                        timeout_env_var,
                        os.getenv("MINERU_OFFICE_CONVERSION_TIMEOUT_SECONDS", "120"),
                    )
                ),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Timed out converting {input_description}: {source_name}"
            ) from exc

        output_path = output_dir / f"{input_path.stem}.{output_suffix}"
        if not output_path.exists():
            candidates = sorted(output_dir.glob(f"*.{output_suffix}"))
            output_path = candidates[0] if candidates else output_path

        if completed.returncode != 0 or not output_path.exists():
            stderr = (completed.stderr or "").strip()
            stdout = (completed.stdout or "").strip()
            detail = stderr or stdout or f"exit code {completed.returncode}"
            raise RuntimeError(
                f"Failed to convert {input_description}: {source_name}. {detail}"
            )

        return output_path.read_bytes()


def convert_doc_bytes_to_docx_bytes(file_bytes: bytes, source_name: str = "document.doc") -> bytes:
    try:
        return _convert_bytes_with_libreoffice(
            file_bytes=file_bytes,
            source_name=source_name,
            input_suffix="doc",
            output_suffix="docx",
            converter_env_var=DOC_CONVERTER_BINARY_ENV,
            timeout_env_var="MINERU_DOC_CONVERSION_TIMEOUT_SECONDS",
            input_description="legacy .doc input",
        )
    except RuntimeError as exc:
        raise LegacyWordConversionError(str(exc)) from exc


def convert_ppt_bytes_to_pptx_bytes(
    file_bytes: bytes,
    source_name: str = "presentation.ppt",
) -> bytes:
    try:
        return _convert_bytes_with_libreoffice(
            file_bytes=file_bytes,
            source_name=source_name,
            input_suffix="ppt",
            output_suffix="pptx",
            converter_env_var=PRESENTATION_CONVERTER_BINARY_ENV,
            timeout_env_var="MINERU_PRESENTATION_CONVERSION_TIMEOUT_SECONDS",
            input_description="legacy .ppt input",
        )
    except RuntimeError as exc:
        raise LegacyPresentationConversionError(str(exc)) from exc


def convert_xls_bytes_to_xlsx_bytes(file_bytes: bytes, source_name: str = "document.xls") -> bytes:
    try:
        return _convert_bytes_with_libreoffice(
            file_bytes=file_bytes,
            source_name=source_name,
            input_suffix="xls",
            output_suffix="xlsx",
            converter_env_var=SPREADSHEET_CONVERTER_BINARY_ENV,
            timeout_env_var="MINERU_SPREADSHEET_CONVERSION_TIMEOUT_SECONDS",
            input_description="legacy .xls input",
        )
    except RuntimeError as exc:
        raise LegacySpreadsheetConversionError(str(exc)) from exc


def _decode_csv_bytes(file_bytes: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("utf-8", errors="replace")


def _guess_csv_dialect(text: str) -> csv.Dialect:
    sample = text[:8192]
    try:
        return csv.Sniffer().sniff(sample)
    except csv.Error:
        return csv.excel


def _safe_worksheet_title(source_name: str) -> str:
    title = re.sub(r"[\[\]\*?:/\\]", "_", Path(source_name).stem or "Sheet1")
    return title[:31] or "Sheet1"


def convert_csv_bytes_to_xlsx_bytes(file_bytes: bytes, source_name: str = "document.csv") -> bytes:
    from openpyxl import Workbook

    text = _decode_csv_bytes(file_bytes)
    reader = csv.reader(StringIO(text), dialect=_guess_csv_dialect(text))
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = _safe_worksheet_title(source_name)

    for row_idx, row in enumerate(reader, start=1):
        for col_idx, value in enumerate(row, start=1):
            cell = worksheet.cell(row=row_idx, column=col_idx)
            cell.value = value
            cell.data_type = "s"

    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def build_hybrid_dependency_error_message(backend: str) -> str:
    return (
        f"`{backend}` requires local pipeline dependencies (`mineru[pipeline]`, "
        "including `torch`). Install `mineru[pipeline]` or `mineru[core]`. "
        "If you need a lightweight remote client without local `torch`, "
        "use `vlm-http-client` instead."
    )


def ensure_backend_dependencies(backend: str) -> None:
    if not backend.startswith("hybrid-"):
        return
    if importlib.util.find_spec("torch") is None:
        raise HybridDependencyError(build_hybrid_dependency_error_message(backend))


def _load_hybrid_analyze_entrypoint(entrypoint_name: str, backend: str):
    """加载统一 hybrid analyze 入口，解析强度由公开 effort 参数控制。"""
    ensure_backend_dependencies(backend)
    module_name = "mineru.backend.hybrid.hybrid_analyze"
    try:
        hybrid_analyze = importlib.import_module(module_name)
    except (ImportError, ModuleNotFoundError) as exc:
        raise HybridDependencyError(
            build_hybrid_dependency_error_message(backend)
        ) from exc
    return getattr(hybrid_analyze, entrypoint_name)


def utf8_byte_length(value: str) -> int:
    return len(value.encode("utf-8"))


def truncate_to_utf8_bytes(value: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""

    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value

    truncated = encoded[:max_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError as exc:
            truncated = truncated[:exc.start]
    return ""


def normalize_task_stem(stem: str, max_bytes: int = MAX_TASK_STEM_BYTES) -> str:
    return truncate_to_utf8_bytes(stem, max_bytes)


def normalize_upload_filename(upload_name: str) -> str:
    sanitized_name = Path(upload_name).name
    sanitized_path = Path(sanitized_name)
    normalized_stem = normalize_task_stem(sanitized_path.stem)
    return f"{normalized_stem}{sanitized_path.suffix}"


def build_task_stem_candidate(
    stem: str,
    suffix: str = "",
    max_bytes: int = MAX_TASK_STEM_BYTES,
) -> str:
    if utf8_byte_length(f"{stem}{suffix}") <= max_bytes:
        return f"{stem}{suffix}"
    suffix_bytes = utf8_byte_length(suffix)
    if suffix_bytes >= max_bytes:
        return truncate_to_utf8_bytes(suffix, max_bytes)
    return f"{truncate_to_utf8_bytes(stem, max_bytes - suffix_bytes)}{suffix}"


def uniquify_task_stems(
    stems: Sequence[str],
) -> tuple[list[str], list[tuple[str, str]]]:
    """Assign task-local unique stems while preserving input order."""
    normalized_inputs = [normalize_task_stem(stem) for stem in stems]
    raw_keys = {stem.casefold() for stem in normalized_inputs}
    occurrence_counts: dict[str, int] = {}
    assigned_keys: set[str] = set()
    unique_stems: list[str] = []
    renamed: list[tuple[str, str]] = []

    for stem, normalized_stem in zip(stems, normalized_inputs):
        stem_base = normalized_stem or stem
        stem_key = stem_base.casefold()
        seen_count = occurrence_counts.get(stem_key, 0)
        occurrence_counts[stem_key] = seen_count + 1

        if seen_count == 0 and stem_key not in assigned_keys:
            effective_stem = stem_base
        else:
            suffix = seen_count + 1
            while True:
                candidate = build_task_stem_candidate(stem_base, f"_{suffix}")
                candidate_key = candidate.casefold()
                if candidate_key not in raw_keys and candidate_key not in assigned_keys:
                    effective_stem = candidate
                    break
                suffix += 1

        assigned_keys.add(effective_stem.casefold())
        unique_stems.append(effective_stem)
        if effective_stem != stem:
            renamed.append((stem, effective_stem))

    return unique_stems, renamed


def read_fn(path, file_suffix: str | None = None):
    if not isinstance(path, Path):
        path = Path(path)
    with open(str(path), "rb") as input_file:
        file_bytes = input_file.read()
        if file_suffix is None:
            file_suffix = guess_suffix_by_bytes(file_bytes, path)
        if file_suffix in image_suffixes:
            return images_bytes_to_pdf_bytes(file_bytes)
        elif file_suffix in doc_suffixes:
            return convert_doc_bytes_to_docx_bytes(file_bytes, source_name=path.name)
        elif file_suffix in ppt_suffixes:
            return convert_ppt_bytes_to_pptx_bytes(file_bytes, source_name=path.name)
        elif file_suffix in xls_suffixes:
            return convert_xls_bytes_to_xlsx_bytes(file_bytes, source_name=path.name)
        elif file_suffix in csv_suffixes:
            return convert_csv_bytes_to_xlsx_bytes(file_bytes, source_name=path.name)
        elif file_suffix in text_input_suffixes:
            return file_bytes
        elif file_suffix in pdf_suffixes + office_suffixes:
            return file_bytes
        else:
            raise Exception(f"Unknown file suffix: {file_suffix}")


def prepare_env(output_dir, pdf_file_name, parse_method):
    local_md_dir = str(os.path.join(output_dir, pdf_file_name, parse_method))
    local_image_dir = os.path.join(str(local_md_dir), "images")
    os.makedirs(local_image_dir, exist_ok=True)
    os.makedirs(local_md_dir, exist_ok=True)
    return local_image_dir, local_md_dir


def _dump_original_image_evidence(
        output_dir,
        pdf_file_names: list[str],
        file_suffixes: list[str] | None,
        original_input_bytes_list: list[bytes | None] | None,
        original_input_names_list: list[str | None] | None,
        backend: str,
        parse_method: str,
):
    """Persist raw uploaded images before MinerU converts them into PDF bytes."""
    if not file_suffixes or not original_input_bytes_list:
        return

    for index, file_suffix in enumerate(file_suffixes):
        if file_suffix not in image_suffixes:
            continue
        if index >= len(pdf_file_names) or index >= len(original_input_bytes_list):
            continue

        original_bytes = original_input_bytes_list[index]
        if not original_bytes:
            continue

        pdf_file_name = pdf_file_names[index]
        original_name = None
        if original_input_names_list is not None and index < len(original_input_names_list):
            original_name = original_input_names_list[index]
        parse_dir = build_parse_dir(
            output_dir,
            pdf_file_name,
            backend,
            parse_method,
        )
        source_dir = parse_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)

        source_filename = f"{pdf_file_name}_original.{file_suffix}"
        source_path = source_dir / source_filename
        source_path.write_bytes(original_bytes)

        manifest = {
            "source_type": "uploaded_image",
            "original_name": original_name or f"{pdf_file_name}.{file_suffix}",
            "stored_path": f"source/{source_filename}",
            "original_suffix": file_suffix,
            "size_bytes": len(original_bytes),
            "sha256": hashlib.sha256(original_bytes).hexdigest(),
            "note": "Raw uploaded image preserved before conversion to PDF bytes for OCR.",
        }
        (source_dir / "source_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )


def convert_pdf_bytes_to_bytes(pdf_bytes, start_page_id=0, end_page_id=None):
    try:
        rebuilt_pdf_bytes = rewrite_pdf_bytes_with_pdfium(
            pdf_bytes,
            start_page_id=start_page_id,
            end_page_id=end_page_id,
        )
        if rebuilt_pdf_bytes:
            return rebuilt_pdf_bytes
        logger.warning(
            "PDFium rewrite returned empty bytes, trying to skip broken pages."
        )
    except Exception as fallback_error:
        logger.warning(
            f"Error in converting PDF bytes with pdfium: {fallback_error}, "
            "trying to skip broken pages."
        )

    try:
        loadable_page_indices, broken_page_indices = get_loadable_pdfium_page_indices(
            pdf_bytes,
            start_page_id=start_page_id,
            end_page_id=end_page_id,
        )
        if broken_page_indices:
            skipped_pages = [page_index + 1 for page_index in broken_page_indices]
            logger.warning(
                f"Skipped broken PDF pages during PDFium rewrite: {skipped_pages}"
            )
        if not loadable_page_indices:
            logger.warning(
                "PDFium skip-broken-page rewrite found no loadable pages, "
                "using original PDF bytes."
            )
            return pdf_bytes

        rebuilt_pdf_bytes = rewrite_pdf_bytes_with_pdfium(
            pdf_bytes,
            start_page_id=start_page_id,
            end_page_id=end_page_id,
            page_indices=loadable_page_indices,
        )
        if rebuilt_pdf_bytes:
            return rebuilt_pdf_bytes
        logger.warning(
            "PDFium skip-broken-page rewrite returned empty bytes, "
            "using original PDF bytes."
        )
    except Exception as fallback_error:
        logger.warning(
            "Error in converting PDF bytes with skip-broken-page fallback: "
            f"{fallback_error}, using original PDF bytes."
        )
    return pdf_bytes


def _prepare_pdf_bytes(pdf_bytes_list, start_page_id, end_page_id):
    """准备处理PDF字节数据"""
    result = []
    for pdf_bytes in pdf_bytes_list:
        new_pdf_bytes = convert_pdf_bytes_to_bytes(pdf_bytes, start_page_id, end_page_id)
        result.append(new_pdf_bytes)
    return result


def _process_output(
        pdf_info,
        pdf_bytes,
        pdf_file_name,
        local_md_dir,
        local_image_dir,
        md_writer,
        f_draw_layout_bbox,
        f_draw_span_bbox,
        f_dump_orig_pdf,
        f_dump_md,
        f_dump_content_list,
        f_dump_middle_json,
        f_dump_model_output,
        f_make_md_mode,
        middle_json,
        model_output=None,
        process_mode="vlm",
        source_suffix: str | None = None,
):
    from mineru.backend.pipeline.pipeline_middle_json_mkcontent import union_make as pipeline_union_make
    from mineru.backend.text.text_middle_json_mkcontent import union_make as text_union_make
    if process_mode == "pipeline":
        make_func = pipeline_union_make
    elif process_mode == "vlm":
        make_func = vlm_union_make
    elif process_mode in office_suffixes:
        make_func = office_union_make
    elif process_mode in text_input_suffixes:
        make_func = text_union_make
    elif process_mode in html_input_suffixes:
        make_func = html_union_make
    else:
        raise Exception(f"Unknown process_mode: {process_mode}")
    """处理输出文件"""
    if f_draw_layout_bbox:
        try:
            draw_layout_bbox(pdf_info, pdf_bytes, local_md_dir, f"{pdf_file_name}_layout.pdf")
        except Exception as exc:
            logger.warning(f"Skipping layout bbox visualization for {pdf_file_name}: {exc}")

    if f_draw_span_bbox:
        try:
            draw_span_bbox(pdf_info, pdf_bytes, local_md_dir, f"{pdf_file_name}_span.pdf")
        except Exception as exc:
            logger.warning(f"Skipping span bbox visualization for {pdf_file_name}: {exc}")

    if f_dump_orig_pdf:
        if process_mode in ["pipeline", "vlm"]:
            md_writer.write(
                f"{pdf_file_name}_origin.pdf",
                pdf_bytes,
            )
        elif process_mode in office_suffixes + text_input_suffixes:
            md_writer.write(
                f"{pdf_file_name}_origin.{process_mode}",
                pdf_bytes,
            )

    image_dir = str(os.path.basename(local_image_dir))

    if f_dump_md:
        md_content_str = make_func(pdf_info, f_make_md_mode, image_dir)
        md_writer.write_string(
            f"{pdf_file_name}.md",
            md_content_str,
        )

    if f_dump_content_list:

        content_list = make_func(pdf_info, MakeMode.CONTENT_LIST, image_dir)
        md_writer.write_string(
            f"{pdf_file_name}_content_list.json",
            json.dumps(content_list, ensure_ascii=False, indent=4),
        )

        content_list_v2 = make_func(pdf_info, MakeMode.CONTENT_LIST_V2, image_dir)
        md_writer.write_string(
            f"{pdf_file_name}_content_list_v2.json",
            json.dumps(content_list_v2, ensure_ascii=False, indent=4),
        )


    if f_dump_middle_json:
        md_writer.write_string(
            f"{pdf_file_name}_middle.json",
            json.dumps(middle_json, ensure_ascii=False, indent=4),
        )

    if f_dump_model_output:
        md_writer.write_string(
            f"{pdf_file_name}_model.json",
            json.dumps(model_output, ensure_ascii=False, indent=4),
        )

    logger.debug(f"local output dir is {local_md_dir}")


def _process_pipeline(
        output_dir,
        pdf_file_names,
        pdf_bytes_list,
        p_lang_list,
        parse_method,
        p_formula_enable,
        p_table_enable,
        f_draw_layout_bbox,
        f_draw_span_bbox,
        f_dump_md,
        f_dump_middle_json,
        f_dump_model_output,
        f_dump_orig_pdf,
        f_dump_content_list,
        f_make_md_mode,
        client_side_output_generation=False,
        file_suffixes: list[str] | None = None,
):
    """处理pipeline后端逻辑"""
    from mineru.backend.pipeline.pipeline_analyze import doc_analyze_streaming as pipeline_doc_analyze_streaming

    image_writer_list = []
    md_writer_list = []
    local_output_info = []
    for idx, pdf_bytes in enumerate(pdf_bytes_list):
        pdf_file_name = pdf_file_names[idx]
        local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, parse_method)
        image_writer, md_writer = FileBasedDataWriter(local_image_dir), FileBasedDataWriter(local_md_dir)
        image_writer_list.append(image_writer)
        md_writer_list.append(md_writer)
        local_output_info.append((pdf_file_name, local_image_dir, local_md_dir))

    output_futures = []

    def run_output_task(doc_index, middle_json, model_list):
        pdf_file_name, local_image_dir, local_md_dir = local_output_info[doc_index]
        md_writer = md_writer_list[doc_index]
        pdf_bytes = pdf_bytes_list[doc_index]
        logger.debug(f"Pipeline output start: doc{doc_index}")
        try:
            source_suffix = (
                file_suffixes[doc_index]
                if file_suffixes is not None and doc_index < len(file_suffixes)
                else None
            )
            _process_output(
                middle_json["pdf_info"], pdf_bytes, pdf_file_name, local_md_dir, local_image_dir,
                md_writer, f_draw_layout_bbox, f_draw_span_bbox, f_dump_orig_pdf,
                f_dump_md, f_dump_content_list, f_dump_middle_json, f_dump_model_output,
                f_make_md_mode, middle_json, model_list, process_mode="pipeline",
                source_suffix=source_suffix,
            )
            logger.debug(f"Pipeline output complete: doc{doc_index}")
        except Exception:
            logger.exception(f"Pipeline output failed: doc{doc_index}")
            raise

    with ThreadPoolExecutor(max_workers=1) as output_executor:
        def on_doc_ready(doc_index, model_list, middle_json, ocr_enable):
            logger.debug(
                f"Pipeline doc ready: doc{doc_index} pages={len(middle_json['pdf_info'])} output_submitted=1"
            )
            future = output_executor.submit(run_output_task, doc_index, middle_json, model_list)
            output_futures.append(future)

        pipeline_doc_analyze_streaming(
            pdf_bytes_list,
            image_writer_list,
            p_lang_list,
            on_doc_ready,
            parse_method=parse_method,
            formula_enable=p_formula_enable,
            table_enable=p_table_enable,
            client_side_output_generation=client_side_output_generation,
        )

        for future in output_futures:
            future.result()
    return


async def _async_process_vlm(
        output_dir,
        pdf_file_names,
        pdf_bytes_list,
        backend,
        f_draw_layout_bbox,
        f_draw_span_bbox,
        f_dump_md,
        f_dump_middle_json,
        f_dump_model_output,
        f_dump_orig_pdf,
        f_dump_content_list,
        f_make_md_mode,
        server_url=None,
        **kwargs,
):
    """异步处理VLM后端逻辑"""
    parse_method = "vlm"
    f_draw_span_bbox = False
    if not backend.endswith("client"):
        server_url = None

    for idx, pdf_bytes in enumerate(pdf_bytes_list):
        pdf_file_name = pdf_file_names[idx]
        local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, parse_method)
        image_writer, md_writer = FileBasedDataWriter(local_image_dir), FileBasedDataWriter(local_md_dir)

        middle_json, infer_result = await aio_vlm_doc_analyze(
            pdf_bytes, image_writer=image_writer, backend=backend, server_url=server_url, **kwargs,
        )

        pdf_info = middle_json["pdf_info"]

        _process_output(
            pdf_info, pdf_bytes, pdf_file_name, local_md_dir, local_image_dir,
            md_writer, f_draw_layout_bbox, f_draw_span_bbox, f_dump_orig_pdf,
            f_dump_md, f_dump_content_list, f_dump_middle_json, f_dump_model_output,
            f_make_md_mode, middle_json, infer_result, process_mode="vlm"
        )


def _process_vlm(
        output_dir,
        pdf_file_names,
        pdf_bytes_list,
        backend,
        f_draw_layout_bbox,
        f_draw_span_bbox,
        f_dump_md,
        f_dump_middle_json,
        f_dump_model_output,
        f_dump_orig_pdf,
        f_dump_content_list,
        f_make_md_mode,
        server_url=None,
        **kwargs,
):
    """同步处理VLM后端逻辑"""
    parse_method = "vlm"
    f_draw_span_bbox = False
    if not backend.endswith("client"):
        server_url = None

    for idx, pdf_bytes in enumerate(pdf_bytes_list):
        pdf_file_name = pdf_file_names[idx]
        local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, parse_method)
        image_writer, md_writer = FileBasedDataWriter(local_image_dir), FileBasedDataWriter(local_md_dir)

        middle_json, infer_result = vlm_doc_analyze(
            pdf_bytes, image_writer=image_writer, backend=backend, server_url=server_url, **kwargs,
        )

        pdf_info = middle_json["pdf_info"]

        _process_output(
            pdf_info, pdf_bytes, pdf_file_name, local_md_dir, local_image_dir,
            md_writer, f_draw_layout_bbox, f_draw_span_bbox, f_dump_orig_pdf,
            f_dump_md, f_dump_content_list, f_dump_middle_json, f_dump_model_output,
            f_make_md_mode, middle_json, infer_result, process_mode="vlm"
        )


def _process_hybrid(
        output_dir,
        pdf_file_names,
        pdf_bytes_list,
        parse_method,
        inline_formula_enable,
        backend,
        f_draw_layout_bbox,
        f_draw_span_bbox,
        f_dump_md,
        f_dump_middle_json,
        f_dump_model_output,
        f_dump_orig_pdf,
        f_dump_content_list,
        f_make_md_mode,
        server_url=None,
        effort=DEFAULT_HYBRID_EFFORT,
        **kwargs,
):
    hybrid_doc_analyze = _load_hybrid_analyze_entrypoint(
        "doc_analyze",
        f"hybrid-{backend}",
    )
    """同步处理hybrid后端逻辑"""
    if not backend.endswith("client"):
        server_url = None

    for idx, pdf_bytes in enumerate(pdf_bytes_list):
        pdf_file_name = pdf_file_names[idx]
        local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, f"hybrid_{parse_method}")
        image_writer, md_writer = FileBasedDataWriter(local_image_dir), FileBasedDataWriter(local_md_dir)

        middle_json, infer_result = hybrid_doc_analyze(
            pdf_bytes,
            image_writer=image_writer,
            backend=backend,
            parse_method=parse_method,
            inline_formula_enable=inline_formula_enable,
            server_url=server_url,
            effort=validate_effort(effort),
            **kwargs,
        )

        pdf_info = middle_json["pdf_info"]

        f_draw_span_bbox = False

        _process_output(
            pdf_info, pdf_bytes, pdf_file_name, local_md_dir, local_image_dir,
            md_writer, f_draw_layout_bbox, f_draw_span_bbox, f_dump_orig_pdf,
            f_dump_md, f_dump_content_list, f_dump_middle_json, f_dump_model_output,
            f_make_md_mode, middle_json, infer_result, process_mode="vlm"
        )


async def _async_process_hybrid(
        output_dir,
        pdf_file_names,
        pdf_bytes_list,
        parse_method,
        inline_formula_enable,
        backend,
        f_draw_layout_bbox,
        f_draw_span_bbox,
        f_dump_md,
        f_dump_middle_json,
        f_dump_model_output,
        f_dump_orig_pdf,
        f_dump_content_list,
        f_make_md_mode,
        server_url=None,
        effort=DEFAULT_HYBRID_EFFORT,
        **kwargs,
):
    aio_hybrid_doc_analyze = _load_hybrid_analyze_entrypoint(
        "aio_doc_analyze",
        f"hybrid-{backend}",
    )
    """异步处理hybrid后端逻辑"""
    if not backend.endswith("client"):
        server_url = None

    for idx, pdf_bytes in enumerate(pdf_bytes_list):
        pdf_file_name = pdf_file_names[idx]
        local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, f"hybrid_{parse_method}")
        image_writer, md_writer = FileBasedDataWriter(local_image_dir), FileBasedDataWriter(local_md_dir)

        middle_json, infer_result = await aio_hybrid_doc_analyze(
            pdf_bytes,
            image_writer=image_writer,
            backend=backend,
            parse_method=parse_method,
            inline_formula_enable=inline_formula_enable,
            server_url=server_url,
            effort=validate_effort(effort),
            **kwargs,
        )

        pdf_info = middle_json["pdf_info"]

        f_draw_span_bbox = False

        _process_output(
            pdf_info, pdf_bytes, pdf_file_name, local_md_dir, local_image_dir,
            md_writer, f_draw_layout_bbox, f_draw_span_bbox, f_dump_orig_pdf,
            f_dump_md, f_dump_content_list, f_dump_middle_json, f_dump_model_output,
            f_make_md_mode, middle_json, infer_result, process_mode="vlm"
        )


def _process_office_doc(
        output_dir,
        pdf_file_names: list[str],
        pdf_bytes_list: list[bytes],
        f_dump_md=True,
        f_dump_middle_json=True,
        f_dump_model_output=True,
        f_dump_orig_file=True,
        f_dump_content_list=True,
        f_make_md_mode=MakeMode.MM_MD,
        file_suffixes: list[str] | None = None,
):
    need_remove_index = []
    for i, file_bytes in enumerate(pdf_bytes_list):
        pdf_file_name = pdf_file_names[i]
        file_suffix = guess_suffix_by_bytes(file_bytes)
        if file_suffix in office_suffixes:

            need_remove_index.append(i)

            local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, f"office")
            image_writer, md_writer = FileBasedDataWriter(local_image_dir), FileBasedDataWriter(local_md_dir)

            if file_suffix in docx_suffixes:
                office_analyze = office_docx_analyze
            elif file_suffix in pptx_suffixes:
                office_analyze = office_pptx_analyze
            elif file_suffix in xlsx_suffixes:
                office_analyze = office_xlsx_analyze
            else:
                raise ValueError(f"Unsupported office suffix: {file_suffix}")

            middle_json, infer_result = office_analyze(
                file_bytes,
                image_writer=image_writer,
            )

            f_draw_layout_bbox = False
            f_draw_span_bbox = False
            pdf_info = middle_json["pdf_info"]
            source_suffix = (
                file_suffixes[i]
                if file_suffixes is not None and i < len(file_suffixes)
                else file_suffix
            )

            _process_output(
                pdf_info, file_bytes, pdf_file_name, local_md_dir, local_image_dir,
                md_writer, f_draw_layout_bbox, f_draw_span_bbox, f_dump_orig_file,
                f_dump_md, f_dump_content_list, f_dump_middle_json, f_dump_model_output,
                f_make_md_mode, middle_json, infer_result, process_mode=file_suffix,
                source_suffix=source_suffix,
            )

    return need_remove_index


def _process_text_doc(
        output_dir,
        pdf_file_names: list[str],
        pdf_bytes_list: list[bytes],
        file_suffixes: list[str] | None = None,
        f_dump_md=True,
        f_dump_middle_json=True,
        f_dump_model_output=True,
        f_dump_orig_file=True,
        f_dump_content_list=True,
        f_make_md_mode=MakeMode.MM_MD,
):
    need_remove_index = []
    for i, file_bytes in enumerate(pdf_bytes_list):
        pdf_file_name = pdf_file_names[i]
        if file_suffixes is not None and i < len(file_suffixes):
            file_suffix = file_suffixes[i]
        else:
            file_suffix = guess_suffix_by_bytes(file_bytes)
        if file_suffix not in text_input_suffixes:
            continue

        need_remove_index.append(i)

        local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, "text")
        md_writer = FileBasedDataWriter(local_md_dir)

        middle_json, infer_result = text_doc_analyze(file_bytes, file_suffix)
        pdf_info = middle_json["pdf_info"]

        _process_output(
            pdf_info, file_bytes, pdf_file_name, local_md_dir, local_image_dir,
            md_writer, False, False, f_dump_orig_file,
            f_dump_md, f_dump_content_list, f_dump_middle_json, f_dump_model_output,
            f_make_md_mode, middle_json, infer_result, process_mode=file_suffix,
            source_suffix=file_suffix,
        )

    return need_remove_index


def _dump_html_url_evidence(local_md_dir: str, pdf_file_name: str, model_output: dict):
    source_dir = Path(local_md_dir) / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_url = str(model_output.get("url") or "")
    (source_dir / "source_url.txt").write_text(source_url, encoding="utf-8")

    manifest = {
        "source_type": "html_url",
        "source_url": source_url,
        "reader_url": model_output.get("reader_url"),
        "title": model_output.get("title"),
        "description": model_output.get("description"),
        "http_status": model_output.get("http_status"),
        "elapsed_seconds": model_output.get("elapsed_seconds"),
        "usage": model_output.get("usage"),
        "stored_path": "source/source_url.txt",
        "note": "HTML URL parsed by Jina Reader API and normalized to MinerU markdown outputs.",
    }
    (source_dir / "source_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )


def _process_html_url_doc(
        output_dir,
        html_url_inputs: list[tuple[str, str]] | None,
        f_dump_md=True,
        f_dump_middle_json=True,
        f_dump_model_output=True,
        f_dump_orig_file=True,
        f_dump_content_list=True,
        f_make_md_mode=MakeMode.MM_MD,
) -> int:
    if not html_url_inputs:
        return 0

    handled_count = 0
    for pdf_file_name, html_url in html_url_inputs:
        local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, "html")
        md_writer = FileBasedDataWriter(local_md_dir)

        middle_json, infer_result = html_url_analyze(html_url)
        pdf_info = middle_json["pdf_info"]

        _process_output(
            pdf_info, html_url.encode("utf-8"), pdf_file_name, local_md_dir, local_image_dir,
            md_writer, False, False, False,
            f_dump_md, f_dump_content_list, f_dump_middle_json, f_dump_model_output,
            f_make_md_mode, middle_json, infer_result, process_mode="html",
            source_suffix="html_url",
        )
        if f_dump_orig_file:
            _dump_html_url_evidence(local_md_dir, pdf_file_name, infer_result)
        handled_count += 1

    return handled_count


def do_parse(
        output_dir,
        pdf_file_names: list[str],
        pdf_bytes_list: list[bytes],
        p_lang_list: list[str],
        backend="pipeline",
        parse_method="auto",
        formula_enable=True,
        table_enable=True,
        server_url=None,
        f_draw_layout_bbox=True,
        f_draw_span_bbox=True,
        f_dump_md=True,
        f_dump_middle_json=True,
        f_dump_model_output=True,
        f_dump_orig_pdf=True,
        f_dump_content_list=True,
        f_make_md_mode=MakeMode.MM_MD,
        start_page_id=0,
        end_page_id=None,
        image_analysis=True,
        client_side_output_generation=False,
        effort=DEFAULT_HYBRID_EFFORT,
        file_suffixes: list[str] | None = None,
        original_input_bytes_list: list[bytes | None] | None = None,
        original_input_names_list: list[str | None] | None = None,
        html_url_inputs: list[tuple[str, str]] | None = None,
        **kwargs,
):
    backend = normalize_backend(backend)
    handled_direct_input_count = 0
    handled_direct_input_count += _process_html_url_doc(
        output_dir,
        html_url_inputs=html_url_inputs,
        f_dump_md=f_dump_md,
        f_dump_middle_json=f_dump_middle_json,
        f_dump_model_output=f_dump_model_output,
        f_dump_orig_file=f_dump_orig_pdf,
        f_dump_content_list=f_dump_content_list,
        f_make_md_mode=f_make_md_mode,
    )
    need_remove_index = _process_text_doc(
        output_dir,
        pdf_file_names=pdf_file_names,
        pdf_bytes_list=pdf_bytes_list,
        file_suffixes=file_suffixes,
        f_dump_md=f_dump_md,
        f_dump_middle_json=f_dump_middle_json,
        f_dump_model_output=f_dump_model_output,
        f_dump_orig_file=f_dump_orig_pdf,
        f_dump_content_list=f_dump_content_list,
        f_make_md_mode=f_make_md_mode,
    )
    handled_direct_input_count += len(need_remove_index)
    for index in sorted(need_remove_index, reverse=True):
        del pdf_bytes_list[index]
        del pdf_file_names[index]
        del p_lang_list[index]
        if file_suffixes is not None and index < len(file_suffixes):
            del file_suffixes[index]
        if original_input_bytes_list is not None and index < len(original_input_bytes_list):
            del original_input_bytes_list[index]
        if original_input_names_list is not None and index < len(original_input_names_list):
            del original_input_names_list[index]
    need_remove_index = _process_office_doc(
        output_dir,
        pdf_file_names=pdf_file_names,
        pdf_bytes_list=pdf_bytes_list,
        f_dump_md=f_dump_md,
        f_dump_middle_json=f_dump_middle_json,
        f_dump_model_output=f_dump_model_output,
        f_dump_orig_file=f_dump_orig_pdf,
        f_dump_content_list=f_dump_content_list,
        f_make_md_mode=f_make_md_mode,
        file_suffixes=file_suffixes,
    )
    handled_direct_input_count += len(need_remove_index)
    for index in sorted(need_remove_index, reverse=True):
        del pdf_bytes_list[index]
        del pdf_file_names[index]
        del p_lang_list[index]
        if file_suffixes is not None and index < len(file_suffixes):
            del file_suffixes[index]
        if original_input_bytes_list is not None and index < len(original_input_bytes_list):
            del original_input_bytes_list[index]
        if original_input_names_list is not None and index < len(original_input_names_list):
            del original_input_names_list[index]
    if not pdf_bytes_list:
        if handled_direct_input_count:
            logger.info("All non-PDF inputs were processed by direct parsers.")
        else:
            logger.warning("No valid PDF, image, Office, text, or HTML URL inputs to process.")
        return

    _dump_original_image_evidence(
        output_dir,
        pdf_file_names=pdf_file_names,
        file_suffixes=file_suffixes,
        original_input_bytes_list=original_input_bytes_list,
        original_input_names_list=original_input_names_list,
        backend=backend,
        parse_method=parse_method,
    )

    # 预处理PDF字节数据
    pdf_bytes_list = _prepare_pdf_bytes(pdf_bytes_list, start_page_id, end_page_id)

    if backend == "pipeline":
        _process_pipeline(
            output_dir, pdf_file_names, pdf_bytes_list, p_lang_list,
            parse_method, formula_enable, table_enable,
            f_draw_layout_bbox, f_draw_span_bbox, f_dump_md, f_dump_middle_json,
            f_dump_model_output, f_dump_orig_pdf, f_dump_content_list, f_make_md_mode,
            client_side_output_generation=client_side_output_generation,
            file_suffixes=file_suffixes,
        )
    else:
        if backend.startswith("vlm-"):
            backend = backend[4:]

            if backend == "engine":
                backend = get_vlm_engine(inference_engine='auto', is_async=False)

            os.environ['MINERU_VLM_FORMULA_ENABLE'] = str(formula_enable)
            os.environ['MINERU_VLM_TABLE_ENABLE'] = str(table_enable)

            _process_vlm(
                output_dir, pdf_file_names, pdf_bytes_list, backend,
                f_draw_layout_bbox, f_draw_span_bbox, f_dump_md, f_dump_middle_json,
                f_dump_model_output, f_dump_orig_pdf, f_dump_content_list, f_make_md_mode,
                server_url, image_analysis=image_analysis,
                client_side_output_generation=client_side_output_generation, **kwargs,
            )
        elif backend.startswith("hybrid-"):
            ensure_backend_dependencies(backend)
            backend = backend[7:]

            if backend == "engine":
                backend = get_vlm_engine(inference_engine='auto', is_async=False)

            os.environ['MINERU_VLM_TABLE_ENABLE'] = str(table_enable)
            os.environ['MINERU_VLM_FORMULA_ENABLE'] = "true"

            _process_hybrid(
                output_dir, pdf_file_names, pdf_bytes_list, parse_method, formula_enable, backend,
                f_draw_layout_bbox, f_draw_span_bbox, f_dump_md, f_dump_middle_json,
                f_dump_model_output, f_dump_orig_pdf, f_dump_content_list, f_make_md_mode,
                server_url, effort=effort, image_analysis=image_analysis,
                client_side_output_generation=client_side_output_generation, **kwargs,
            )


async def aio_do_parse(
        output_dir,
        pdf_file_names: list[str],
        pdf_bytes_list: list[bytes],
        p_lang_list: list[str],
        backend="pipeline",
        parse_method="auto",
        formula_enable=True,
        table_enable=True,
        server_url=None,
        f_draw_layout_bbox=True,
        f_draw_span_bbox=True,
        f_dump_md=True,
        f_dump_middle_json=True,
        f_dump_model_output=True,
        f_dump_orig_pdf=True,
        f_dump_content_list=True,
        f_make_md_mode=MakeMode.MM_MD,
        start_page_id=0,
        end_page_id=None,
        image_analysis=True,
        client_side_output_generation=False,
        effort=DEFAULT_HYBRID_EFFORT,
        file_suffixes: list[str] | None = None,
        original_input_bytes_list: list[bytes | None] | None = None,
        original_input_names_list: list[str | None] | None = None,
        html_url_inputs: list[tuple[str, str]] | None = None,
        **kwargs,
):
    backend = normalize_backend(backend)
    handled_direct_input_count = 0
    handled_direct_input_count += await asyncio.to_thread(
        _process_html_url_doc,
        output_dir,
        html_url_inputs=html_url_inputs,
        f_dump_md=f_dump_md,
        f_dump_middle_json=f_dump_middle_json,
        f_dump_model_output=f_dump_model_output,
        f_dump_orig_file=f_dump_orig_pdf,
        f_dump_content_list=f_dump_content_list,
        f_make_md_mode=f_make_md_mode,
    )
    need_remove_index = await asyncio.to_thread(
        _process_text_doc,
        output_dir,
        pdf_file_names=pdf_file_names,
        pdf_bytes_list=pdf_bytes_list,
        file_suffixes=file_suffixes,
        f_dump_md=f_dump_md,
        f_dump_middle_json=f_dump_middle_json,
        f_dump_model_output=f_dump_model_output,
        f_dump_orig_file=f_dump_orig_pdf,
        f_dump_content_list=f_dump_content_list,
        f_make_md_mode=f_make_md_mode,
    )
    handled_direct_input_count += len(need_remove_index)
    for index in sorted(need_remove_index, reverse=True):
        del pdf_bytes_list[index]
        del pdf_file_names[index]
        del p_lang_list[index]
        if file_suffixes is not None and index < len(file_suffixes):
            del file_suffixes[index]
        if original_input_bytes_list is not None and index < len(original_input_bytes_list):
            del original_input_bytes_list[index]
        if original_input_names_list is not None and index < len(original_input_names_list):
            del original_input_names_list[index]

    # Office 解析是同步且可能耗时的操作，异步入口需要放到线程中避免阻塞事件循环。
    need_remove_index = await asyncio.to_thread(
        _process_office_doc,
        output_dir,
        pdf_file_names=pdf_file_names,
        pdf_bytes_list=pdf_bytes_list,
        f_dump_md=f_dump_md,
        f_dump_middle_json=f_dump_middle_json,
        f_dump_model_output=f_dump_model_output,
        f_dump_orig_file=f_dump_orig_pdf,
        f_dump_content_list=f_dump_content_list,
        f_make_md_mode=f_make_md_mode,
        file_suffixes=file_suffixes,
    )
    handled_direct_input_count += len(need_remove_index)
    for index in sorted(need_remove_index, reverse=True):
        del pdf_bytes_list[index]
        del pdf_file_names[index]
        del p_lang_list[index]
        if file_suffixes is not None and index < len(file_suffixes):
            del file_suffixes[index]
        if original_input_bytes_list is not None and index < len(original_input_bytes_list):
            del original_input_bytes_list[index]
        if original_input_names_list is not None and index < len(original_input_names_list):
            del original_input_names_list[index]
    if not pdf_bytes_list:
        if handled_direct_input_count:
            logger.info("All non-PDF inputs were processed by direct parsers.")
        else:
            logger.warning("No valid PDF, image, Office, text, or HTML URL inputs to process.")
        return

    _dump_original_image_evidence(
        output_dir,
        pdf_file_names=pdf_file_names,
        file_suffixes=file_suffixes,
        original_input_bytes_list=original_input_bytes_list,
        original_input_names_list=original_input_names_list,
        backend=backend,
        parse_method=parse_method,
    )

    # 预处理PDF字节数据
    pdf_bytes_list = _prepare_pdf_bytes(pdf_bytes_list, start_page_id, end_page_id)

    if backend == "pipeline":
        # pipeline模式暂不支持异步，使用同步处理方式
        _process_pipeline(
            output_dir, pdf_file_names, pdf_bytes_list, p_lang_list,
            parse_method, formula_enable, table_enable,
            f_draw_layout_bbox, f_draw_span_bbox, f_dump_md, f_dump_middle_json,
            f_dump_model_output, f_dump_orig_pdf, f_dump_content_list, f_make_md_mode,
            client_side_output_generation=client_side_output_generation,
            file_suffixes=file_suffixes,
        )
    else:
        if backend.startswith("vlm-"):
            backend = backend[4:]

            if backend == "engine":
                backend = get_vlm_engine(inference_engine='auto', is_async=True)

            os.environ['MINERU_VLM_FORMULA_ENABLE'] = str(formula_enable)
            os.environ['MINERU_VLM_TABLE_ENABLE'] = str(table_enable)

            await _async_process_vlm(
                output_dir, pdf_file_names, pdf_bytes_list, backend,
                f_draw_layout_bbox, f_draw_span_bbox, f_dump_md, f_dump_middle_json,
                f_dump_model_output, f_dump_orig_pdf, f_dump_content_list, f_make_md_mode,
                server_url, image_analysis=image_analysis,
                client_side_output_generation=client_side_output_generation, **kwargs,
            )
        elif backend.startswith("hybrid-"):
            ensure_backend_dependencies(backend)
            backend = backend[7:]

            if backend == "engine":
                backend = get_vlm_engine(inference_engine='auto', is_async=True)

            os.environ['MINERU_VLM_TABLE_ENABLE'] = str(table_enable)
            os.environ['MINERU_VLM_FORMULA_ENABLE'] = "true"

            await _async_process_hybrid(
                output_dir, pdf_file_names, pdf_bytes_list, parse_method, formula_enable, backend,
                f_draw_layout_bbox, f_draw_span_bbox, f_dump_md, f_dump_middle_json,
                f_dump_model_output, f_dump_orig_pdf, f_dump_content_list, f_make_md_mode,
                server_url, effort=effort, image_analysis=image_analysis,
                client_side_output_generation=client_side_output_generation, **kwargs,
            )


if __name__ == "__main__":
    # pdf_path = "../../demo/pdfs/demo3.pdf"
    pdf_path = "C:/Users/zhaoxiaomeng/Downloads/4546d0e2-ba60-40a5-a17e-b68555cec741.pdf"

    try:
       do_parse("./output", [Path(pdf_path).stem], [read_fn(Path(pdf_path))],["ch"],
                end_page_id=10,
                backend='vlm-huggingface'
                # backend = 'pipeline'
                )
    except Exception as e:
        logger.exception(e)
