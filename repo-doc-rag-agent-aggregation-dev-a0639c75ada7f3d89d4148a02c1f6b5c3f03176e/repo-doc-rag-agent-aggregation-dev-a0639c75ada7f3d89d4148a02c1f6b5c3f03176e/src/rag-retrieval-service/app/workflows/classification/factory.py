from __future__ import annotations

from typing import Protocol

from app.config.settings import Settings
from app.llm.gateway import LLMGateway
from app.workflows.classification.robust_strategy import RobustLLMClassificationStrategy
from app.workflows.classification.strategies import (
    ClassificationRun,
    ClassificationService,
    FastLLMClassificationStrategy,
    RuleClassificationStrategy,
)


class ClassificationRunner(Protocol):
    async def classify(
        self,
        *,
        request_id: str,
        query: str | list[str],
        scope_document_count: int = 0,
        debug_enabled: bool = False,
    ) -> ClassificationRun: ...


def build_classification_strategy(
    *, settings: Settings, gateway: LLMGateway
) -> ClassificationRunner:
    if settings.rag_query_classification_strategy == "fast":
        return ClassificationService(
            primary=FastLLMClassificationStrategy(
                gateway=gateway,
                model=settings.rag_llm_model,
                max_tokens=settings.rag_llm_max_output_tokens,
            ),
            fallback=RuleClassificationStrategy(),
        )
    return RobustLLMClassificationStrategy(
        gateway=gateway,
        model=settings.rag_llm_model,
        max_tokens=settings.rag_llm_max_output_tokens,
    )
