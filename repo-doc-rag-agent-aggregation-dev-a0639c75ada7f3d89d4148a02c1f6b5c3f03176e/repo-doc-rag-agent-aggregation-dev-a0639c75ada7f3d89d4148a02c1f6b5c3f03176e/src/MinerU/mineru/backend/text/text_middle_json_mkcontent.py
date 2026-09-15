# Copyright (c) Opendatalab. All rights reserved.
from mineru.utils.enum_class import BlockType, ContentType, ContentTypeV2, MakeMode


def _iter_text_blocks(pdf_info_dict: list):
    for page_info in pdf_info_dict:
        page_idx = page_info.get("page_idx", 0)
        for block in page_info.get("para_blocks") or []:
            if block.get("type") != BlockType.TEXT:
                continue
            yield page_idx, block


def _collect_block_spans(block: dict) -> list[dict]:
    spans = []
    for line in block.get("lines") or []:
        for span in line.get("spans") or []:
            content = span.get("content")
            if content is None:
                continue
            spans.append(span)
    return spans


def _block_text(block: dict) -> str:
    return "".join(str(span.get("content", "")) for span in _collect_block_spans(block))


def _legacy_content_list(pdf_info_dict: list) -> list[dict]:
    content_list = []
    for page_idx, block in _iter_text_blocks(pdf_info_dict):
        text = _block_text(block)
        if not text:
            continue
        content_list.append(
            {
                "type": BlockType.TEXT,
                "text": text,
                "page_idx": page_idx,
            }
        )
    return content_list


def _content_list_v2(pdf_info_dict: list) -> list[list[dict]]:
    output = []
    for page_info in pdf_info_dict:
        page_contents = []
        for block in page_info.get("para_blocks") or []:
            if block.get("type") != BlockType.TEXT:
                continue
            spans = []
            for span in _collect_block_spans(block):
                span_type = span.get("type")
                if span_type not in {ContentType.TEXT, ContentTypeV2.SPAN_MD}:
                    span_type = ContentType.TEXT
                spans.append(
                    {
                        "type": span_type,
                        "content": span.get("content", ""),
                    }
                )
            if spans:
                page_contents.append(
                    {
                        "type": ContentTypeV2.PARAGRAPH,
                        "content": {
                            "paragraph_content": spans,
                        },
                    }
                )
        output.append(page_contents)
    return output


def union_make(pdf_info_dict: list, make_mode: str, img_buket_path: str = ""):
    del img_buket_path

    if make_mode in [MakeMode.MM_MD, MakeMode.NLP_MD]:
        blocks = [
            _block_text(block).strip("\r\n")
            for _, block in _iter_text_blocks(pdf_info_dict)
            if _block_text(block)
        ]
        return "\n\n".join(blocks)
    if make_mode == MakeMode.CONTENT_LIST:
        return _legacy_content_list(pdf_info_dict)
    if make_mode == MakeMode.CONTENT_LIST_V2:
        return _content_list_v2(pdf_info_dict)
    return None

