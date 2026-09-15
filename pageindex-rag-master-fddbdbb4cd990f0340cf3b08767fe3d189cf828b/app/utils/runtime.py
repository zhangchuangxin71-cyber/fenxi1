# 模块说明：运行时对象，封装缓存、熔断器与统计信息。
import asyncio
import copy
import threading
import time
from collections import OrderedDict

from assistant_components import BM25Prefilter
from prompts.defaults import PromptSet

DEFAULT_PAGE_CONTENT_CACHE_SIZE = 512
DEFAULT_PDF_IO_CONCURRENCY = 2
DEFAULT_QA_CACHE_SIZE = 256
DEFAULT_QA_CACHE_TTL_SECONDS = 600
DEFAULT_LLM_BREAKER_FAIL_THRESHOLD = 5
DEFAULT_LLM_BREAKER_COOLDOWN_SECONDS = 60


class PageContentLRUCache:
    """页面内容 LRU 缓存。"""
    def __init__(self, maxsize: int = DEFAULT_PAGE_CONTENT_CACHE_SIZE):
        """初始化缓存容量与统计字段。"""
        self.maxsize = max(16, int(maxsize))
        self._store: OrderedDict[tuple[str, str, str, str], list[dict]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple[str, str, str, str]) -> list[dict] | None:
        """读取缓存命中项（返回深拷贝，避免外部修改污染缓存）。"""
        with self._lock:
            value = self._store.get(key)
            if value is None:
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            return copy.deepcopy(value)

    def set(self, key: tuple[str, str, str, str], value: list[dict]):
        """写入缓存并维护 LRU 淘汰。"""
        with self._lock:
            self._store[key] = copy.deepcopy(value)
            self._store.move_to_end(key)
            if len(self._store) > self.maxsize:
                self._store.popitem(last=False)


class TTLResultCache:
    """带 TTL 的结果缓存。"""
    def __init__(self, maxsize: int = DEFAULT_QA_CACHE_SIZE, ttl_seconds: int = DEFAULT_QA_CACHE_TTL_SECONDS):
        """初始化缓存大小、过期时间与统计字段。"""
        self.maxsize = max(16, int(maxsize))
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._store: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict | None:
        """读取未过期缓存项。"""
        now = time.time()
        with self._lock:
            cached = self._store.get(key)
            if cached is None:
                self.misses += 1
                return None
            expires_at, payload = cached
            if now > expires_at:
                self._store.pop(key, None)
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            return copy.deepcopy(payload)

    def set(self, key: str, payload: dict):
        """写入缓存并在超过容量时淘汰最早项。"""
        with self._lock:
            self._store[key] = (time.time() + self.ttl_seconds, copy.deepcopy(payload))
            self._store.move_to_end(key)
            while len(self._store) > self.maxsize:
                self._store.popitem(last=False)


class LLMCircuitBreaker:
    """LLM 调用熔断器。"""
    def __init__(self, fail_threshold: int = 5, cooldown_seconds: int = 60):
        """初始化阈值、冷却时间和统计计数。"""
        self.fail_threshold = fail_threshold
        self.cooldown_seconds = cooldown_seconds
        self.fail_count = 0
        self.opened_at: float | None = None
        self.open_events = 0
        self.rejected_requests = 0
        self.failure_events = 0
        self.success_events = 0

    def allow(self) -> bool:
        """判断当前是否允许继续发起 LLM 请求。"""
        if self.opened_at is None:
            return True
        if (time.time() - self.opened_at) >= self.cooldown_seconds:
            self.fail_count = 0
            self.opened_at = None
            return True
        self.rejected_requests += 1
        return False

    def on_success(self):
        """记录成功事件并关闭熔断状态。"""
        self.fail_count = 0
        self.opened_at = None
        self.success_events += 1

    def on_failure(self):
        """记录失败事件；达到阈值后开启熔断。"""
        self.fail_count += 1
        self.failure_events += 1
        if self.fail_count >= self.fail_threshold and self.opened_at is None:
            self.opened_at = time.time()
            self.open_events += 1


class AssistantRuntime:
    """助手运行时上下文（缓存、熔断、统计、并发控制）。"""
    def __init__(
        self,
        *,
        prompts: PromptSet | None = None,
        page_cache_size: int = DEFAULT_PAGE_CONTENT_CACHE_SIZE,
        breaker_fail_threshold: int = DEFAULT_LLM_BREAKER_FAIL_THRESHOLD,
        breaker_cooldown_seconds: int = DEFAULT_LLM_BREAKER_COOLDOWN_SECONDS,
        pdf_io_concurrency: int = DEFAULT_PDF_IO_CONCURRENCY,
    ):
        """初始化运行时组件与默认统计指标。"""
        self.prompts = prompts or PromptSet()
        self.page_content_cache = PageContentLRUCache(maxsize=page_cache_size)
        self.qa_result_cache = TTLResultCache()
        self.llm_breaker = LLMCircuitBreaker(
            fail_threshold=breaker_fail_threshold,
            cooldown_seconds=breaker_cooldown_seconds,
        )
        self.pdf_io_semaphore = asyncio.Semaphore(max(1, int(pdf_io_concurrency)))
        self.bm25_prefilter_cache: tuple[str, BM25Prefilter] | None = None
        self.stats: dict[str, int | float] = {
            "index_success": 0,
            "index_fail": 0,
            "qa_requests": 0,
            "qa_failures": 0,
            "llm_requests": 0,
            "llm_timeouts": 0,
        }
        # Keep a tiny in-memory turn history for anaphora resolution in fallback QA.
        self.recent_turns: list[dict[str, str]] = []


def get_client_runtime(client) -> AssistantRuntime:
    """获取或懒加载 client 绑定的运行时对象。"""
    runtime = getattr(client, "_assistant_runtime", None)
    if runtime is None:
        runtime = AssistantRuntime()
        setattr(client, "_assistant_runtime", runtime)
    return runtime

