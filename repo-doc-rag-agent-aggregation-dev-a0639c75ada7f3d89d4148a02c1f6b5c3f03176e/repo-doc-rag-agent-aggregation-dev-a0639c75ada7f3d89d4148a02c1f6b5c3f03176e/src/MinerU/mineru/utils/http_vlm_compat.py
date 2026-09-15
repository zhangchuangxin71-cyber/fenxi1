# Copyright (c) Opendatalab. All rights reserved.
import os

from mineru.utils.vlm_usage import record_vlm_usage


_PATCHED = False


def _is_ark_client(client) -> bool:
    provider = os.getenv("MINERU_VL_PROVIDER", "").strip().lower()
    if provider in {"ark", "volcengine", "volces"}:
        return True
    server_url = getattr(client, "server_url", "") or ""
    return "ark.cn-beijing.volces.com" in server_url


def apply_http_vlm_compat() -> None:
    """Patch mineru-vl-utils HTTP client for OpenAI-compatible providers.

    mineru-vl-utils assumes a `/v1` route under the configured server URL.
    Volcengine Ark uses the configured `/api/v3` base URL directly.
    """
    global _PATCHED
    if _PATCHED:
        return

    try:
        from mineru_vl_utils.vlm_client.http_client import HttpVlmClient
    except Exception:
        return

    original_chat_url = HttpVlmClient.chat_url.fget
    original_build_request_body = HttpVlmClient.build_request_body
    original_get_response_data = HttpVlmClient.get_response_data

    @property
    def patched_chat_url(self) -> str:
        if _is_ark_client(self):
            return f"{self.server_url}/chat/completions"
        return original_chat_url(self)

    def patched_build_request_body(self, *args, **kwargs) -> dict:
        request_body = original_build_request_body(self, *args, **kwargs)
        if _is_ark_client(self):
            for key in (
                "priority",
                "repetition_penalty",
                "skip_special_tokens",
                "top_k",
                "vllm_xargs",
            ):
                request_body.pop(key, None)
        return request_body

    def patched_get_response_data(self, response) -> dict:
        response_data = original_get_response_data(self, response)
        try:
            record_vlm_usage(response_data, client=self, response=response)
        except Exception:
            pass
        return response_data

    HttpVlmClient.chat_url = patched_chat_url
    HttpVlmClient.build_request_body = patched_build_request_body
    HttpVlmClient.get_response_data = patched_get_response_data
    _PATCHED = True
