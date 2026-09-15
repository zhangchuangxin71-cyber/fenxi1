from __future__ import annotations

import os

from app.config.settings import Settings
from app.container import AppContainer
from app.workflows.classification.robust_strategy import RobustLLMClassificationStrategy
from app.workflows.classification.strategies import ClassificationService


def test_container_constructs_single_shared_gateway_limiter_pool_and_executor() -> None:
    settings = Settings(_env_file=None, postgres_dsn="postgresql://unused", ark_api_key="x")
    container = AppContainer.build(settings)

    assert container.graph_services.gateway is container.gateway
    assert container.direct_factory.gateway is container.gateway
    assert container.focused_factory.gateway is container.gateway
    assert isinstance(container.classifier, RobustLLMClassificationStrategy)
    assert container.classifier.gateway is container.gateway
    assert container.router.gateway is container.gateway
    assert container.limiter is not None
    assert container.pool is not None
    assert container.db_executor is not None
    assert container.documents.repository is container.repository
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"


def test_container_can_select_fast_classification_without_changing_graph_services() -> None:
    settings = Settings(
        _env_file=None,
        postgres_dsn="postgresql://unused",
        ark_api_key="x",
        rag_query_classification_strategy="fast",
    )

    container = AppContainer.build(settings)

    assert isinstance(container.classifier, ClassificationService)
    assert container.classifier.primary.gateway is container.gateway
    assert container.graph_services.classifier is container.classifier
