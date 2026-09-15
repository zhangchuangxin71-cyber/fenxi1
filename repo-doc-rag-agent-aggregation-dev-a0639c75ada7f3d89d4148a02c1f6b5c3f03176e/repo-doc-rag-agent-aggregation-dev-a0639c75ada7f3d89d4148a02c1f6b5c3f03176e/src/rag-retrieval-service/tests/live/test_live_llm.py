from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from app.llm.circuit_breaker import LLMCircuitBreaker
from app.llm.gateway import LLMGateway
from app.llm.provider import OpenAICompatibleProvider
from app.workflows.classification.strategies import LLMClassificationStrategy


def _llm_config() -> dict[str, str]:
    values = dict(os.environ)
    env_path = os.getenv("RAG_TEST_ENV_FILE", "")
    if env_path and Path(env_path).is_file():
        values.update({key: str(value) for key, value in dotenv_values(env_path).items() if value})
    required = ("ARK_API_KEY", "ARK_BASE_URL", "RAG_LLM_MODEL")
    if not all(values.get(key) for key in required):
        pytest.skip("Ark smoke-test configuration is not available")
    return {key: values[key] for key in required}


@pytest.mark.live_llm
@pytest.mark.asyncio
async def test_live_ark_supports_four_category_structured_classification() -> None:
    config = _llm_config()
    provider = OpenAICompatibleProvider(
        api_key=config["ARK_API_KEY"],
        base_url=config["ARK_BASE_URL"],
        timeout_seconds=30,
        max_retries=0,
    )
    gateway = LLMGateway(
        provider=provider,
        max_concurrency=2,
        max_queued_calls=8,
        per_request_max_in_flight=2,
        circuit_breaker=LLMCircuitBreaker(enabled=False),
    )
    classifier = LLMClassificationStrategy(
        gateway=gateway, model=config["RAG_LLM_MODEL"], max_tokens=1600
    )
    gateway.begin_request("live-classification", debug_enabled=True)
    try:
        output = await classifier.classify(
            request_id="live-classification",
            scope_document_count=8,
            query=(
                "你能看到哪些文档？查看A公司年报第12页；详细总结B公司年报；"
                "A公司与B公司去年营业额谁多"
            ),
        )
        stats = gateway.stats("live-classification")
    finally:
        await gateway.close()
        await provider.close()

    assert output.scope_direct
    assert output.routed_direct
    assert output.routed_broad
    assert output.routed_focused
    focused_questions = [question for group in output.routed_focused for question in group.queries]
    assert any("A公司" in question and "B公司" not in question for question in focused_questions)
    assert any("B公司" in question and "A公司" not in question for question in focused_questions)
    assert stats.request_count == 1
    assert stats.total_tokens > 0
