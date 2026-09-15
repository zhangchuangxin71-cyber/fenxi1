# Copyright (c) Opendatalab. All rights reserved.
from pathlib import Path


OFFICE_PARSE_DIR_NAME = "office"
TEXT_PARSE_DIR_NAME = "text"
HTML_PARSE_DIR_NAME = "html"
VLM_PARSE_DIR_NAME = "vlm"


def build_parse_dir(
    output_dir: str | Path,
    pdf_name: str,
    backend: str,
    parse_method: str,
    *,
    is_office: bool = False,
    is_text: bool = False,
    is_html: bool = False,
) -> Path:
    output_root = Path(output_dir)
    if is_office:
        return output_root / pdf_name / OFFICE_PARSE_DIR_NAME
    if is_text:
        return output_root / pdf_name / TEXT_PARSE_DIR_NAME
    if is_html:
        return output_root / pdf_name / HTML_PARSE_DIR_NAME
    if backend.startswith("pipeline"):
        return output_root / pdf_name / parse_method
    if backend.startswith("vlm"):
        return output_root / pdf_name / VLM_PARSE_DIR_NAME
    if backend.startswith("hybrid"):
        return output_root / pdf_name / f"hybrid_{parse_method}"
    raise ValueError(f"Unknown backend type: {backend}")


def resolve_parse_dir(
    output_dir: str | Path,
    pdf_name: str,
    backend: str,
    parse_method: str,
    *,
    is_office: bool = False,
    is_text: bool = False,
    is_html: bool = False,
    allow_office_fallback: bool = False,
    allow_text_fallback: bool = False,
    allow_html_fallback: bool = False,
) -> Path:
    parse_dir = build_parse_dir(
        output_dir,
        pdf_name,
        backend,
        parse_method,
        is_office=is_office,
        is_text=is_text,
        is_html=is_html,
    )
    if is_office or is_text or is_html:
        return parse_dir

    if not parse_dir.exists():
        if allow_office_fallback:
            office_dir = build_parse_dir(
                output_dir,
                pdf_name,
                backend,
                parse_method,
                is_office=True,
            )
            if office_dir.exists():
                return office_dir
        if allow_text_fallback:
            text_dir = build_parse_dir(
                output_dir,
                pdf_name,
                backend,
                parse_method,
                is_text=True,
            )
            if text_dir.exists():
                return text_dir
        if allow_html_fallback:
            html_dir = build_parse_dir(
                output_dir,
                pdf_name,
                backend,
                parse_method,
                is_html=True,
            )
            if html_dir.exists():
                return html_dir
    return parse_dir
