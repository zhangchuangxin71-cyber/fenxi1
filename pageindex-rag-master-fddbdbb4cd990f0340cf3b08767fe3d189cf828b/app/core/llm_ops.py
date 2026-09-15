# 模块说明：LLM 调用封装，含重试、超时、熔断与兜底回答。
import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from pageindex.utils import extract_json, llm_completion

from core.common import log_event
from utils.runtime import LLMCircuitBreaker, get_client_runtime

logger = logging.getLogger(__name__)

DEFAULT_LLM_RETRIES = 3
_DEFAULT_LLM_TIMEOUT_FROM_ENV = str(os.getenv("DA_LLM_TIMEOUT_SECONDS", "30") or "30").strip()
try:
    DEFAULT_LLM_TIMEOUT_SECONDS = int(_DEFAULT_LLM_TIMEOUT_FROM_ENV)
except Exception:
    DEFAULT_LLM_TIMEOUT_SECONDS = 30
DEFAULT_LLM_BREAKER_FAIL_THRESHOLD = 5
DEFAULT_LLM_BREAKER_COOLDOWN_SECONDS = 60
DEFAULT_NO_INFO_ANSWER = "文档中未提供相关信息，重新调用模型进行通识回答。"
GENERAL_KNOWLEDGE_SOURCE_LABEL = "【信息来源：通用常识（非当前文档）】"


# 单次同步 LLM 调用封装，供异步线程池调用。
def _run_llm_completion_once(model: str | None, prompt: str) -> str:
    """执行一次同步 LLM 调用（无重试、无超时）。"""
    return llm_completion(model=model, prompt=prompt)


# 安全 LLM 调用：带超时、重试、指数退避和熔断保护。
async def safe_llm_completion_async(
    model: str | None,
    prompt: str,
    *,
    retries: int = DEFAULT_LLM_RETRIES,
    timeout_seconds: int = DEFAULT_LLM_TIMEOUT_SECONDS,
    breaker: LLMCircuitBreaker | None = None,
    runtime=None,
) -> str:
    """带重试/超时/熔断保护的异步 LLM 调用入口。

    调用策略：
    - 先判断熔断器是否允许请求；
    - 每次失败按指数退避重试；
    - 达到最大重试后返回空字符串，由上层决定兜底行为。
    """
    effective_breaker = breaker or LLMCircuitBreaker(
        fail_threshold=DEFAULT_LLM_BREAKER_FAIL_THRESHOLD,
        cooldown_seconds=DEFAULT_LLM_BREAKER_COOLDOWN_SECONDS,
    )
    if not effective_breaker.allow():
        log_event(
            "llm_breaker_reject",
            level=logging.ERROR,
            model=model or "",
            retries=retries,
        )
        return ""

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        started = time.perf_counter()
        if runtime is not None:
            runtime.stats["llm_requests"] = int(runtime.stats.get("llm_requests", 0)) + 1
        try:
            if int(timeout_seconds) > 0:
                async with asyncio.timeout(int(timeout_seconds)):
                    result = await asyncio.to_thread(_run_llm_completion_once, model, prompt)
            else:
                # timeout_seconds <= 0 means disabling timeout for this call.
                result = await asyncio.to_thread(_run_llm_completion_once, model, prompt)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            log_event(
                "llm_call_ok",
                level=logging.DEBUG,
                model=model or "",
                attempt=attempt,
                elapsed_ms=elapsed_ms,
            )
            effective_breaker.on_success()
            return (result or "").strip()
        except Exception as exc:
            last_error = exc
            effective_breaker.on_failure()
            if isinstance(exc, TimeoutError) and runtime is not None:
                runtime.stats["llm_timeouts"] = int(runtime.stats.get("llm_timeouts", 0)) + 1
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            if attempt < retries:
                sleep_seconds = 2 ** (attempt - 1)
                log_event(
                    "llm_call_retry",
                    level=logging.WARNING,
                    model=model or "",
                    attempt=attempt,
                    retries=retries,
                    elapsed_ms=elapsed_ms,
                    sleep_seconds=sleep_seconds,
                    error=str(exc),
                )
                await asyncio.sleep(sleep_seconds)
            else:
                log_event(
                    "llm_call_failed",
                    level=logging.ERROR,
                    model=model or "",
                    retries=retries,
                    elapsed_ms=elapsed_ms,
                    error=str(exc),
                )
    if last_error:
        logger.exception(last_error)
    return ""


# 对外统一的异步 LLM 调用入口。
async def async_llm_completion(
    model: str | None,
    prompt: str,
    *,
    retries: int = DEFAULT_LLM_RETRIES,
    timeout_seconds: int = DEFAULT_LLM_TIMEOUT_SECONDS,
    breaker: LLMCircuitBreaker | None = None,
    runtime=None,
) -> str:
    """对外统一的异步 LLM 调用包装。"""
    return await safe_llm_completion_async(
        model=model,
        prompt=prompt,
        retries=retries,
        timeout_seconds=timeout_seconds,
        breaker=breaker,
        runtime=runtime,
    )


# 容错提取 JSON：优先 extract_json，失败则尝试清理代码块再解析。
def extract_json_tolerant(raw_text: str, default: dict[str, Any]) -> dict[str, Any]:
    """容错解析 JSON 文本。

    优先使用 `extract_json`，失败后再尝试去掉代码块标记并截取首尾花括号解析。
    """
    if not raw_text:
        return default

    try:
        parsed = extract_json(raw_text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidate = cleaned[first_brace:last_brace + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    return default


# 判断回答是否属于“无信息/未命中”类型。
def is_no_info_answer(answer: str | None) -> bool:
    """判断回答是否属于“文档未命中/无信息”类型。"""
    text = (answer or "").strip()
    if not text:
        return True
    if text == DEFAULT_NO_INFO_ANSWER:
        return True
    lowered = re.sub(r"\s+", "", text.lower())
    no_info_patterns = (
        "文档中未提供",
        "未提供相关信息",
        "未找到相关信息",
        "没有相关信息",
        "无法从文档中找到",
    )
    if any(pattern in lowered for pattern in no_info_patterns):
        return True
    return False


# 给通识兜底答案追加来源标签，便于和文档证据答案区分。
def format_general_knowledge_answer(answer: str) -> str:
    """为通识兜底答案补充来源标签。"""
    cleaned = (answer or "").strip()
    if not cleaned:
        return DEFAULT_NO_INFO_ANSWER
    if cleaned.startswith(GENERAL_KNOWLEDGE_SOURCE_LABEL):
        return cleaned
    return f"{GENERAL_KNOWLEDGE_SOURCE_LABEL}\n{cleaned}"


# 文档检索未命中时，生成带来源声明的通识兜底回答。
async def generate_general_knowledge_answer(client, query: str, dialogue_context: str = "") -> str:
    """在文档检索未命中时生成通识兜底回答。"""
    runtime = get_client_runtime(client)
    context_block = ""
    if dialogue_context:
        context_block = (
            "以下是同一会话最近对话（可用于通用指代消解，如“这个/那个/它/前者/后者”）：\n"
            f"{dialogue_context}\n\n"
        )
    prompt = (
        "你是一个中文问答助手。当前文档检索未命中可用证据。"
        "请基于通用常识回答用户问题，并满足以下要求：\n"
        "1) 明确写出该回答来源于通用常识而非当前文档；\n"
        "2) 若不确定，请直接说明不确定并给出最稳妥说法；\n"
        "2.1) 若用户问题存在指代（如“这个/那个/它/前者/后者”），优先结合最近对话中的明确对象；\n"
        "3) 回答简洁、直接，不编造具体数据来源。\n\n"
        f"{context_block}"
        f"用户问题：{query}\n"
    )
    answer = await safe_llm_completion_async(
        model=client.retrieve_model,
        prompt=prompt,
        breaker=runtime.llm_breaker,
        runtime=runtime,
    )
    if not answer:
        return format_general_knowledge_answer(
            "当前未能获取通用常识回答（模型调用失败或超时）。请稍后重试，或直接使用联网搜索。"
        )
    return format_general_knowledge_answer(answer)

