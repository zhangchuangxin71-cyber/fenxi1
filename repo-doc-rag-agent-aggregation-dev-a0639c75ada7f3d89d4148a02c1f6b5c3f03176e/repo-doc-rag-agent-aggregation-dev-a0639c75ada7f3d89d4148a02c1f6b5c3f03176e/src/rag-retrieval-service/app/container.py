from __future__ import annotations

import os
from dataclasses import dataclass

from app.budgeting.tokens import TiktokenCounter
from app.config.settings import Settings
from app.db.executor import DatabaseExecutor
from app.db.pool import PostgresPool
from app.db.repositories import RetrievalRepository
from app.documents.repair_client import RawRepairClient
from app.documents.service import DocumentService
from app.graph.builder import RetrievalGraphServices, build_retrieval_graph
from app.llm.circuit_breaker import LLMCircuitBreaker
from app.llm.gateway import LLMGateway
from app.llm.provider import OpenAICompatibleProvider
from app.security.admission import RetrievalAdmissionController
from app.security.rate_limit import TokenBucketLimiter
from app.service import RetrievalEngine
from app.tools.direct import DirectToolExecutor
from app.tools.node_tree import NodeTreeScanner
from app.workflows.broad_retrieval.service import RuleBroadRetrievalStrategy
from app.workflows.classification.factory import (
    ClassificationRunner,
    build_classification_strategy,
)
from app.workflows.direct_access.planner import LLMDirectPlanner, RuleDirectPlanner
from app.workflows.direct_access.service import DirectAccessService
from app.workflows.document_routing.endpoint import DocumentRouteEndpointService
from app.workflows.document_routing.prefilter import KeywordPrefilterService
from app.workflows.document_routing.service import DocumentRoutingService
from app.workflows.focused_search.optional_possible import OptionalFocusedPossibleService
from app.workflows.focused_search.page_inspector import PageInspector
from app.workflows.focused_search.service import FocusedSearchService
from app.workflows.merge.service import merge_candidates
from app.workflows.scope_access.planner import LLMScopePlanner, RuleScopePlanner
from app.workflows.scope_access.service import ScopeAccessService


@dataclass(slots=True)
class DirectAccessFactory:
    repository: RetrievalRepository
    gateway: LLMGateway
    settings: Settings
    db_executor: DatabaseExecutor

    def for_request(self, request) -> DirectAccessService:
        executor = DirectToolExecutor(
            repository=self.repository,
            user_id=request.user_id,
            kb_id=request.kb_id,
            session_id=request.session_id,
        )
        return DirectAccessService(
            primary=LLMDirectPlanner(
                gateway=self.gateway,
                model=self.settings.rag_llm_model,
                max_tokens=min(1024, self.settings.rag_llm_max_output_tokens),
            ),
            fallback=RuleDirectPlanner(),
            executor=executor,
            db_executor=self.db_executor,
        )


@dataclass(slots=True)
class FocusedSearchFactory:
    repository: RetrievalRepository
    gateway: LLMGateway
    settings: Settings
    token_counter: TiktokenCounter
    db_executor: DatabaseExecutor

    def for_request(self, request) -> FocusedSearchService:
        scanner = NodeTreeScanner(
            repository=self.repository,
            user_id=request.user_id,
            kb_id=request.kb_id,
            count_tokens=self.token_counter,
            max_result_tokens=self.settings.rag_tree_scan_max_tokens,
            session_id=request.session_id,
        )
        inspector = PageInspector(
            repository=self.repository,
            gateway=self.gateway,
            model=self.settings.rag_llm_model,
            context_window=self.settings.rag_llm_context_window,
            output_tokens=min(2048, self.settings.rag_llm_max_output_tokens),
            safety_margin=self.settings.rag_llm_safety_margin_tokens,
            count_tokens=self.token_counter,
            accept_score=self.settings.rag_page_accept_score,
            possible_score=self.settings.rag_page_possible_score,
            db_executor=self.db_executor,
            session_id=request.session_id,
        )
        return FocusedSearchService(
            repository=self.repository,
            scanner=scanner,
            page_inspector=inspector,
            gateway=self.gateway,
            model=self.settings.rag_llm_model,
            max_tree_steps=self.settings.rag_tree_max_steps,
            tree_output_tokens=min(1024, self.settings.rag_llm_max_output_tokens),
            db_executor=self.db_executor,
            session_id=request.session_id,
        )


@dataclass(slots=True)
class AppContainer:
    settings: Settings
    pool: PostgresPool
    db_executor: DatabaseExecutor
    repository: RetrievalRepository
    token_counter: TiktokenCounter
    provider: OpenAICompatibleProvider
    gateway: LLMGateway
    limiter: TokenBucketLimiter
    admission: RetrievalAdmissionController
    classifier: ClassificationRunner
    router: DocumentRoutingService
    direct_factory: DirectAccessFactory
    focused_factory: FocusedSearchFactory
    graph_services: RetrievalGraphServices
    graph: object
    engine: RetrievalEngine
    documents: DocumentService
    document_routes: DocumentRouteEndpointService
    raw_repair_client: RawRepairClient

    @classmethod
    def build(cls, settings: Settings) -> AppContainer:
        # This service uses its own response debug trace and never uploads runs.
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        pool_min, pool_max = settings.normalized_pool_bounds()
        pool = PostgresPool(
            dsn=settings.postgres_dsn,
            min_size=pool_min,
            max_size=pool_max,
            acquire_timeout_seconds=settings.db_pool_acquire_timeout_seconds,
            query_timeout_seconds=settings.db_query_timeout_seconds,
            connect_timeout_seconds=settings.db_connect_timeout_seconds,
            idle_ttl_seconds=settings.db_pool_idle_ttl_seconds,
            reaper_interval_seconds=settings.db_pool_reaper_interval_seconds,
        )
        db_executor = DatabaseExecutor(max_workers=pool_max)
        repository = RetrievalRepository(pool)
        token_counter = TiktokenCounter()
        provider = OpenAICompatibleProvider(
            api_key=settings.ark_api_key,
            base_url=settings.ark_base_url,
            timeout_seconds=settings.rag_llm_timeout_seconds,
            max_retries=settings.rag_llm_max_retries,
        )
        gateway = LLMGateway(
            provider=provider,
            max_concurrency=settings.rag_llm_max_concurrency,
            max_queued_calls=settings.rag_llm_max_queued_calls,
            per_request_max_in_flight=settings.rag_llm_per_request_max_in_flight,
            circuit_breaker=LLMCircuitBreaker(
                enabled=settings.rag_llm_circuit_breaker_enabled,
                failure_threshold=settings.rag_llm_circuit_failure_threshold,
                recovery_seconds=settings.rag_llm_circuit_recovery_seconds,
            ),
        )
        classifier = build_classification_strategy(settings=settings, gateway=gateway)
        router = DocumentRoutingService(
            gateway=gateway,
            model=settings.rag_llm_model,
            context_window=settings.rag_llm_context_window,
            output_tokens=settings.rag_llm_max_output_tokens,
            safety_margin=settings.rag_llm_safety_margin_tokens,
            count_tokens=token_counter,
            prefilter_threshold=settings.rag_document_prefilter_threshold,
        )
        direct_factory = DirectAccessFactory(repository, gateway, settings, db_executor)
        focused_factory = FocusedSearchFactory(
            repository, gateway, settings, token_counter, db_executor
        )
        optional_possible = OptionalFocusedPossibleService(
            repository=repository,
            count_tokens=token_counter,
            minimum_page_tokens=settings.rag_minimum_page_tokens,
            possible_score=settings.rag_page_possible_score,
        )
        broad = RuleBroadRetrievalStrategy(
            repository=repository,
            count_tokens=token_counter,
            minimum_chunk_tokens=settings.rag_minimum_page_tokens,
        )
        graph_services = RetrievalGraphServices(
            repository=repository,
            classifier=classifier,
            keyword_prefilter=KeywordPrefilterService(
                threshold=settings.rag_document_prefilter_threshold,
                max_candidates=settings.rag_document_prefilter_max_candidates,
                count_tokens=token_counter,
            ),
            router=router,
            scope_access=ScopeAccessService(
                primary=LLMScopePlanner(
                    gateway=gateway,
                    model=settings.rag_llm_model,
                    max_tokens=min(512, settings.rag_llm_max_output_tokens),
                ),
                fallback=RuleScopePlanner(),
            ),
            direct_access=direct_factory,
            focused_search=focused_factory,
            optional_possible=optional_possible,
            broad_retrieval=broad,
            gateway=gateway,
            merge=merge_candidates,
            count_tokens=token_counter,
            default_return_tokens=settings.rag_default_return_tokens,
            db_executor=db_executor,
        )
        graph = build_retrieval_graph(graph_services)
        engine = RetrievalEngine(
            graph=graph,
            gateway=gateway,
            settings=settings,
            count_tokens=token_counter,
            tokenizer_name=token_counter.encoding_name,
            merge=merge_candidates,
        )
        raw_repair_client = RawRepairClient(
            base_url=settings.ingestion_service_base_url,
            timeout_seconds=settings.ingestion_repair_timeout_seconds,
        )
        documents = DocumentService(
            repository=repository,
            db_executor=db_executor,
            max_doc_ids=settings.rag_max_doc_ids,
            repair_client=raw_repair_client,
            api_prefix=settings.rag_api_prefix,
        )
        document_routes = DocumentRouteEndpointService(
            repository=repository,
            db_executor=db_executor,
            keyword_prefilter=graph_services.keyword_prefilter,
            router=router,
            gateway=gateway,
            max_doc_ids=settings.rag_max_doc_ids,
        )
        limiter = TokenBucketLimiter(
            enabled=settings.rag_rate_limit_enabled,
            per_minute=settings.rag_rate_limit_per_minute,
            burst=settings.rag_rate_limit_burst,
            idle_ttl_seconds=settings.rag_rate_limit_bucket_ttl_seconds,
        )
        admission = RetrievalAdmissionController(
            max_active=settings.app_max_concurrency,
            max_queued=settings.app_max_queued_requests,
            wait_timeout_seconds=settings.app_admission_wait_timeout_seconds,
        )
        return cls(
            settings=settings,
            pool=pool,
            db_executor=db_executor,
            repository=repository,
            token_counter=token_counter,
            provider=provider,
            gateway=gateway,
            limiter=limiter,
            admission=admission,
            classifier=classifier,
            router=router,
            direct_factory=direct_factory,
            focused_factory=focused_factory,
            graph_services=graph_services,
            graph=graph,
            engine=engine,
            documents=documents,
            document_routes=document_routes,
            raw_repair_client=raw_repair_client,
        )

    async def start(self) -> None:
        if not self.settings.postgres_dsn:
            raise RuntimeError("POSTGRES_DSN is required")
        await self.db_executor.run(self.pool.open)

    async def ready(self) -> None:
        await self.db_executor.run(self.pool.ping)

    async def close(self) -> None:
        await self.raw_repair_client.close()
        await self.gateway.close()
        await self.provider.close()
        self.pool.close()
        self.db_executor.close()
