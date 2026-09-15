# Copyright (c) Opendatalab. All rights reserved.
from mineru.utils.enum_class import BlockType, ContentType, ContentTypeV2
from mineru.version import __version__


def decode_text_bytes(file_bytes: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("utf-8", errors="replace")


def normalize_text_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def text_doc_analyze(file_bytes: bytes, file_suffix: str):
    text = normalize_text_newlines(decode_text_bytes(file_bytes))
    normalized_suffix = file_suffix.lower()
    is_markdown = normalized_suffix in {"md", "markdown"}
    span_type = ContentTypeV2.SPAN_MD if is_markdown else ContentType.TEXT
    source_format = "markdown" if is_markdown else "text"

    para_blocks = []
    if text:
        para_blocks.append(
            {
                "type": BlockType.TEXT,
                "lines": [
                    {
                        "spans": [
                            {
                                "type": span_type,
                                "content": text,
                            }
                        ]
                    }
                ],
                "index": 0,
                "source_format": source_format,
            }
        )

    middle_json = {
        "pdf_info": [
            {
                "para_blocks": para_blocks,
                "discarded_blocks": [],
                "page_idx": 0,
            }
        ],
        "_backend": "text",
        "_version_name": __version__,
    }
    model_output = {
        "type": source_format,
        "content": text,
    }
    return middle_json, model_output

