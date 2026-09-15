import asyncio
import logging

from app.utils.errors import FrontendServiceError

logger = logging.getLogger(__name__)


async def safe_run_graph(graph, state: dict, emitter):
    try:
        return await graph.ainvoke(
            state,
            config={"configurable": {"emitter": emitter}, "recursion_limit": 25},
        )
    except asyncio.CancelledError:
        raise
    except FrontendServiceError as exc:
        logger.error("graph execution failed with frontend service error: %s", exc.internal_message, exc_info=True)
        await emitter.emit_error(exc.status_code, exc.error_type, exc.public_message, fatal=True)
        return {}
    except Exception:
        logger.exception("graph execution failed")
        await emitter.emit_error(500, "internal_error", "服务暂时不可用，请稍后再试。", fatal=True)
        return {}


async def safe_llm_call(coro, *, emitter, fallback_text: str = "（生成失败，已为您返回缺省内容）"):
    try:
        return await coro
    except asyncio.TimeoutError:
        if emitter:
            await emitter.emit_error(500, "llm_timeout", "模型响应超时", fatal=False)
            await emitter.emit_text_delta(fallback_text)
        return None
    except Exception:
        logger.exception("llm call failed")
        if emitter:
            await emitter.emit_error(500, "llm_error", "模型调用异常", fatal=False)
            await emitter.emit_text_delta(fallback_text)
        return None
