import asyncio
import json
import logging
import os
import time
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from volcenginesdkarkruntime import AsyncArk
except Exception:
    AsyncArk = None

from .exceptions import DependencyError, SummaryError, TimeoutError
from .text_utils import clean_text
from .tree_utils import TreeUtil

ARK_API_KEY = os.getenv("ARK_API_KEY")
ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
DEFAULT_ARK_MODEL = (os.getenv("ARK_MODEL") or os.getenv("ARK_ENDPOINT_ID") or "").strip() or None
DEFAULT_SYSTEM_PROMPT = "You are a document summarizer. Output JSON only."
DEFAULT_PROMPT_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "doubao_summary_prompt.txt"

_async_ark_client = None


class _GlobalRateLimiter:
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


_rate_limiters: dict[float, _GlobalRateLimiter] = {}
_rate_limiters_lock = threading.Lock()


def _get_global_rate_limiter(rate_limit_per_sec: float) -> _GlobalRateLimiter | None:
    rate = float(rate_limit_per_sec)
    if rate <= 0:
        return None
    key = round(rate, 6)
    with _rate_limiters_lock:
        limiter = _rate_limiters.get(key)
        if limiter is None:
            limiter = _GlobalRateLimiter(rate)
            _rate_limiters[key] = limiter
        return limiter


def get_async_ark_client() -> Any:
    """Return shared async Ark client instance."""
    global _async_ark_client
    if AsyncArk is None:
        raise DependencyError("Missing volcengine sdk. Install: pip install volcengine-python-sdk[ark]")
    if _async_ark_client is None:
        _async_ark_client = AsyncArk(api_key=ARK_API_KEY, base_url=ARK_BASE_URL)
    return _async_ark_client



def _load_prompt_template(template_path: Optional[str]) -> str:
    path = Path(template_path) if template_path else DEFAULT_PROMPT_TEMPLATE_PATH
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return (
            "Read the input and produce a dense summary. "
            "Capture the key entities and topics. "
            "Keep the summary within {max_chars} characters. "
            "Output JSON only in the form {\"summary\":\"...\"}.\n"
            "Input: {input_text}"
        )


class BaseSummarizer(ABC):
    """Provider-level summarizer abstraction."""

    def __init__(self, summary_input_chars: int, summary_max_chars: int) -> None:
        self.summary_input_chars = max(100, int(summary_input_chars))
        self.summary_max_chars = max(20, int(summary_max_chars))

    @abstractmethod
    async def summarize(self, text: str) -> str:
        """Summarize one input text without retry/rate-limit orchestration."""


class DoubaoSummarizer(BaseSummarizer):
    """Doubao (Volcengine Ark) provider adapter."""

    def __init__(
        self,
        summary_input_chars: int,
        summary_max_chars: int,
        model: Optional[str] = None,
        temperature: float = 0.1,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        prompt_template_path: Optional[str] = None,
    ) -> None:
        super().__init__(summary_input_chars=summary_input_chars, summary_max_chars=summary_max_chars)
        self.model = (model or DEFAULT_ARK_MODEL or "").strip() or None
        self.temperature = float(temperature)
        self.system_prompt = system_prompt
        self.prompt_template = _load_prompt_template(prompt_template_path)

    def _build_user_prompt(self, text: str) -> str:
        """Render prompt template with configured limits and input text."""
        prompt = self.prompt_template
        prompt = prompt.replace("{max_chars}", str(self.summary_max_chars))
        prompt = prompt.replace("{input_text}", text[: self.summary_input_chars])
        return prompt

    async def summarize(self, text: str) -> str:
        """Call Doubao model once and parse JSON summary response."""
        if not clean_text(text):
            raise SummaryError("Summary source text is empty.", stage="summary_prepare", retryable=False)
        if not self.model:
            raise SummaryError(
                "ARK model is not configured. Set ARK_MODEL/ARK_ENDPOINT_ID.",
                stage="summary_prepare",
                retryable=False,
            )

        client = get_async_ark_client()
        user_prompt = self._build_user_prompt(text)
        resp = await client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self.temperature,
            max_tokens=max(300, int(self.summary_max_chars * 2)),
            thinking={"type": "disabled"},
        )

        content = clean_text(resp.choices[0].message.content)
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end <= start:
            raise SummaryError("LLM response is not valid JSON.", stage="summary_parse")

        data = json.loads(content[start : end + 1])
        summary = clean_text(data.get("summary", ""))
        if not summary:
            raise SummaryError("LLM response summary is empty.", stage="summary_parse")
        return summary


class SummarizationInvoker:
    """Unified calling layer for timeout/retry/rate-limit."""

    def __init__(
        self,
        provider: BaseSummarizer,
        timeout_seconds: float,
        max_retries: int,
        retry_backoff_base: float,
        rate_limit_per_sec: float,
    ) -> None:
        self.provider = provider
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = max(1, int(max_retries))
        self.retry_backoff_base = max(0.0, float(retry_backoff_base))
        self.rate_limit_per_sec = max(0.0, float(rate_limit_per_sec))
        self._rate_limiter = _get_global_rate_limiter(self.rate_limit_per_sec)
        self._call_count = 0
        self._failure_count = 0

    async def _acquire_rate_limit(self) -> None:
        """Throttle request pace across summarizer instances in this worker process."""
        if self._rate_limiter is None:
            return
        await asyncio.to_thread(self._rate_limiter.wait)

    async def invoke(self, text: str) -> str:
        """Invoke provider with unified timeout/retry/rate-limit policy."""
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                await self._acquire_rate_limit()
                self._call_count += 1
                if self.timeout_seconds > 0:
                    try:
                        return await asyncio.wait_for(self.provider.summarize(text), timeout=self.timeout_seconds)
                    except asyncio.TimeoutError as e:
                        raise TimeoutError("Summary request timed out.", stage="summary_timeout", cause=e) from e
                return await self.provider.summarize(text)
            except Exception as e:
                last_error = e
                self._failure_count += 1
                logging.warning("Summary invoke failed attempt=%s/%s: %s", attempt, self.max_retries, e)
                if attempt < self.max_retries and self.retry_backoff_base > 0:
                    await asyncio.sleep(self.retry_backoff_base * attempt)
        if isinstance(last_error, SummaryError):
            raise last_error
        if isinstance(last_error, TimeoutError):
            raise SummaryError(str(last_error), stage="summary_timeout", cause=last_error)
        if last_error:
            raise SummaryError(f"Summary invoke failed: {last_error}", stage="summary_invoke", cause=last_error)
        raise SummaryError("Summary invoke failed with unknown error", stage="summary_invoke")

    def get_metrics(self) -> Dict[str, int]:
        """Expose invocation counters for structured monitoring."""
        return {
            "llm_calls": int(self._call_count),
            "llm_failures": int(self._failure_count),
        }


class TreeSummarizer:
    """Tree-level summary composition using provider + invoker."""

    def __init__(
        self,
        invoker: SummarizationInvoker,
        summary_concurrency: int,
        summary_input_chars: int,
        summary_max_chars: int,
        min_summary_text_chars: int,
    ) -> None:
        self.invoker = invoker
        self.summary_concurrency = max(1, int(summary_concurrency))
        self.summary_input_chars = max(100, int(summary_input_chars))
        self.summary_max_chars = max(20, int(summary_max_chars))
        self.min_summary_text_chars = max(1, int(min_summary_text_chars))

    def _get_summary_source_text(self, node: Dict[str, Any]) -> str:
        """Build model input text from node summary source/title/body."""
        source = clean_text(node.get("_summary_source"))
        if source:
            return source

        title = clean_text(node.get("title"))
        text = clean_text(node.get("text"))

        base_parts: List[str] = []
        if title:
            base_parts.append(f"Title: {title}")
        if text and text != title:
            base_parts.append(f"Content: {text}")

        combined = clean_text("\n".join(base_parts))
        if combined:
            return combined[: self.summary_input_chars]
        return title

    def _collect_leaf_nodes_for_summary(self, nodes: List[Dict[str, Any]], output: List[Dict[str, Any]]) -> None:
        """Collect all leaf nodes for LLM summarization phase."""
        for node, _, _ in TreeUtil.iter_leaves(nodes):
            output.append(node)

    def _build_local_summary(self, node: Dict[str, Any], text: str) -> str:
        """Build a fast local summary for short leaf nodes without calling LLM."""
        title = clean_text(node.get("title"))
        normalized_text = clean_text(text)
        if title and normalized_text and normalized_text != title:
            combined = f"{title}：{normalized_text}"
        else:
            combined = title or normalized_text

        return clean_text(combined)[: self.summary_max_chars]

    def _build_fallback_summary(self, node: Dict[str, Any], text: str) -> str:
        """Fallback to truncated source text when LLM summary generation fails."""
        fallback = self._build_local_summary(node, text)
        if fallback:
            return fallback

        title = clean_text(node.get("title"))
        if title:
            return title[: self.summary_max_chars]

        return "摘要生成失败，已使用原文截断替代。"[: self.summary_max_chars]

    def _compose_parent_summaries(self, nodes: List[Dict[str, Any]]) -> None:
        """Compose parent summaries from child titles only."""
        for node, _, _ in TreeUtil.iter_postorder(nodes):
            child_nodes = TreeUtil.children(node)
            if not child_nodes:
                continue

            child_titles: List[str] = []
            for child in child_nodes:
                child_label = clean_text(child.get("title")) or clean_text(child.get("summary"))
                if child_label:
                    child_titles.append(child_label[: self.summary_max_chars])

            if not child_titles:
                node["summary"] = self._build_fallback_summary(node, self._get_summary_source_text(node))
                node.pop("keywords", None)
                logging.warning(
                    "Parent summary fallback used: node_id=%s title=%s",
                    node.get("node_id"),
                    clean_text(node.get("title"))[:80],
                )
                continue

            parent_summary = clean_text("；".join(child_titles))
            node["summary"] = parent_summary[: self.summary_max_chars]
            node.pop("keywords", None)

    async def generate_summaries_async(self, nodes: List[Dict[str, Any]]) -> None:
        """Generate summaries for leaves, then compose parent summaries."""
        leaf_targets: List[Dict[str, Any]] = []
        self._collect_leaf_nodes_for_summary(nodes, leaf_targets)
        if not leaf_targets:
            return

        semaphore = asyncio.Semaphore(self.summary_concurrency)
        progress_lock = asyncio.Lock()

        async def _one(node: Dict[str, Any], total: int, state: Dict[str, int]) -> None:
            text = self._get_summary_source_text(node)
            if not text:
                node["summary"] = self._build_fallback_summary(node, "")
                node.pop("keywords", None)
                logging.warning(
                    "Leaf summary source missing, fallback used: node_id=%s title=%s",
                    node.get("node_id"),
                    clean_text(node.get("title"))[:80],
                )
                async with progress_lock:
                    state["done"] += 1
                return

            normalized_text = clean_text(text)
            started_at = time.perf_counter()
            summary_mode = "local"
            if len(normalized_text) <= self.min_summary_text_chars:
                node["summary"] = self._build_local_summary(node, normalized_text)
                node.pop("keywords", None)
            else:
                summary_mode = "llm"
                async with semaphore:
                    try:
                        summary = await self.invoker.invoke(normalized_text)
                        node["summary"] = summary
                    except Exception as exc:
                        node["summary"] = self._build_fallback_summary(node, normalized_text)
                        summary_mode = "fallback"
                        logging.warning(
                            "Leaf summary fallback used: node_id=%s error=%s title=%s",
                            node.get("node_id"),
                            exc,
                            clean_text(node.get("title"))[:80],
                        )
                    node.pop("keywords", None)

            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logging.info(
                "Leaf summary done: node_id=%s mode=%s input_chars=%s elapsed_ms=%s title=%s",
                node.get("node_id"),
                summary_mode,
                len(normalized_text),
                elapsed_ms,
                clean_text(node.get("title"))[:80],
            )

            async with progress_lock:
                state["done"] += 1
                done = state["done"]
                if done % 5 == 0 or done == total:
                    logging.info("Leaf summary progress: %s/%s", done, total)

        logging.info("Generating summaries for leaf nodes only, nodes=%s", len(leaf_targets))
        state = {"done": 0}
        await asyncio.gather(*[_one(node, len(leaf_targets), state) for node in leaf_targets])
        self._compose_parent_summaries(nodes)

    def _collect_doc_summary_inputs(self, nodes: List[Dict[str, Any]], limit: int = 8) -> List[str]:
        """Collect early node summaries as document-level routing evidence."""
        inputs: List[str] = []
        for node, _, _ in TreeUtil.iter_preorder(nodes):
            summary = clean_text(node.get("summary"))
            if summary:
                inputs.append(summary)
            if len(inputs) >= limit:
                break
        return inputs

    async def generate_document_description_async(self, structure: Dict[str, Any]) -> str:
        """Generate a document-level description from early node summaries."""
        nodes = structure.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            return ""

        summary_inputs = self._collect_doc_summary_inputs(nodes, limit=8)
        if not summary_inputs:
            return ""

        doc_name = clean_text(structure.get("doc_name")) or "document"
        doc_type = clean_text(structure.get("doc_type")) or "document"
        page_count = structure.get("page_count")
        page_text = f"共{page_count}页" if isinstance(page_count, int) and page_count > 0 else ""
        source_text = clean_text(
            "\n".join(
                [
                    f"文档名称：{doc_name}",
                    f"文档类型：{doc_type}",
                    page_text,
                    "以下是文档前几个节点的摘要，请生成一段用于文档级检索路由的整体描述：",
                    *[f"{idx + 1}. {item}" for idx, item in enumerate(summary_inputs)],
                ]
            )
        )
        description = await self.invoker.invoke(source_text[: self.summary_input_chars])
        return clean_text(description)

    def ensure_summary_for_nodes(self, nodes: List[Dict[str, Any]]) -> None:
        """Validate all nodes have summary after summarization."""
        for node, _, _ in TreeUtil.iter_preorder(nodes):
            existing = clean_text(node.get("summary"))
            if not existing:
                raise SummaryError(
                    f"Summary missing for node_id={node.get('node_id')}, summary must be generated before validation.",
                    stage="summary_validation",
                    retryable=False,
                )

    def get_metrics(self) -> Dict[str, int]:
        """Expose LLM invocation metrics for pipeline-level logging."""
        return self.invoker.get_metrics()



def build_doubao_tree_summarizer(
    summary_input_chars: int,
    summary_max_chars: int,
    summary_concurrency: int,
    min_summary_text_chars: int,
    summary_rate_limit_per_sec: float,
    summary_timeout_seconds: float,
    summary_retry_times: int,
    summary_retry_backoff_base: float,
    summary_prompt_template: Optional[str] = None,
    summary_model: Optional[str] = None,
) -> TreeSummarizer:
    """Build default tree summarizer stack for Doubao provider."""
    provider = DoubaoSummarizer(
        summary_input_chars=summary_input_chars,
        summary_max_chars=summary_max_chars,
        model=summary_model,
        prompt_template_path=summary_prompt_template,
    )
    invoker = SummarizationInvoker(
        provider=provider,
        timeout_seconds=summary_timeout_seconds,
        max_retries=summary_retry_times,
        retry_backoff_base=summary_retry_backoff_base,
        rate_limit_per_sec=summary_rate_limit_per_sec,
    )
    return TreeSummarizer(
        invoker=invoker,
        summary_concurrency=summary_concurrency,
        summary_input_chars=summary_input_chars,
        summary_max_chars=summary_max_chars,
        min_summary_text_chars=min_summary_text_chars,
    )
