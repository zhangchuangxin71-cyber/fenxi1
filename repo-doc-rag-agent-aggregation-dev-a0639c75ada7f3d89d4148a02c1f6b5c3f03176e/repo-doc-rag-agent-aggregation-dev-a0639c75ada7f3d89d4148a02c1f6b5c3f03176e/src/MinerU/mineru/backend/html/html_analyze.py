# Copyright (c) Opendatalab. All rights reserved.
import os
from urllib.parse import urlparse

from mineru.backend.html.jina_reader import call_jina_reader
from mineru.utils.enum_class import BlockType, ContentTypeV2
from mineru.version import __version__


def _env_flag_enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    if value < minimum:
        return default
    return value


def normalize_html_url(url: str) -> str:
    normalized_url = url.strip()
    parsed = urlparse(normalized_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid HTML URL: {url}")
    return normalized_url


def normalize_text_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def html_url_analyze(
    url: str,
    timeout: int | None = None,
    no_cache: bool | None = None,
    remove_images: bool | None = None,
    use_api_key: bool | None = None,
):
    normalized_url = normalize_html_url(url)
    extraction = call_jina_reader(
        target_url=normalized_url,
        timeout=timeout
        if timeout is not None
        else _env_int("MINERU_HTML_JINA_TIMEOUT_SECONDS", 60, minimum=5),
        no_cache=no_cache
        if no_cache is not None
        else _env_flag_enabled("MINERU_HTML_JINA_NO_CACHE"),
        remove_images=remove_images
        if remove_images is not None
        else _env_flag_enabled("MINERU_HTML_JINA_REMOVE_IMAGES"),
        use_api_key=use_api_key
        if use_api_key is not None
        else _env_flag_enabled("MINERU_HTML_JINA_USE_API_KEY", default=True),
    )
    markdown = normalize_text_newlines(extraction["markdown"])

    para_blocks = []
    if markdown:
        para_blocks.append(
            {
                "type": BlockType.TEXT,
                "lines": [
                    {
                        "spans": [
                            {
                                "type": ContentTypeV2.SPAN_MD,
                                "content": markdown,
                            }
                        ]
                    }
                ],
                "index": 0,
                "source_format": "html_url",
                "source_url": normalized_url,
            }
        )

    middle_json = {
        "pdf_info": [
            {
                "para_blocks": para_blocks,
                "discarded_blocks": [],
                "page_idx": 0,
                "source_url": normalized_url,
            }
        ],
        "_backend": "html",
        "_source_type": "html_url",
        "_source_url": normalized_url,
        "_version_name": __version__,
    }
    model_output = {
        "type": "html_url",
        **extraction,
    }
    return middle_json, model_output

