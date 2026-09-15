from __future__ import annotations

from app.config.settings import Settings
from app.workflows.classification.factory import build_classification_strategy
from app.workflows.classification.robust_strategy import RobustLLMClassificationStrategy
from app.workflows.classification.strategies import ClassificationService


class Gateway:
    pass


def test_factory_selects_one_whole_classification_strategy_from_process_config() -> None:
    gateway = Gateway()

    robust = build_classification_strategy(
        settings=Settings(_env_file=None, rag_query_classification_strategy="robust"),
        gateway=gateway,
    )
    fast = build_classification_strategy(
        settings=Settings(_env_file=None, rag_query_classification_strategy="fast"),
        gateway=gateway,
    )

    assert isinstance(robust, RobustLLMClassificationStrategy)
    assert robust.gateway is gateway
    assert isinstance(fast, ClassificationService)
    assert fast.primary.gateway is gateway
