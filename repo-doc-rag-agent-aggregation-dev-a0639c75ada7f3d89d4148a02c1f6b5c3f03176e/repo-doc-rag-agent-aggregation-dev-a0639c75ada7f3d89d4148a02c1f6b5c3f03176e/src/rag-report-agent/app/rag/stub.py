from app.config import settings
from app.rag.types import ProvidedChunkInput
from app.rag.postgres_retriever import retrieve as postgres_retrieve
from app.rag.remote_retriever import retrieve as remote_retrieve


async def retrieve(
    *,
    kb_id: str,
    user_id: str,
    query: str,
    session_id: str,
    doc_ids: list[str] | None = None,
    temp_doc_ids: list[str] | None = None,
    top_k: int = 5,
    search_mode: str = "hybrid",
    ensure_document_coverage: bool = False,
) -> list[ProvidedChunkInput]:
    """Retrieve chunks from the configured RAG backend."""
    if settings.rag_retrieval_backend == "remote":
        return await remote_retrieve(
            base_url=settings.rag_retrieval_service_url,
            kb_id=kb_id,
            user_id=user_id,
            query=query,
            session_id=session_id,
            doc_ids=doc_ids,
            temp_doc_ids=temp_doc_ids,
            top_k=top_k,
            search_mode=search_mode,
            ensure_document_coverage=ensure_document_coverage,
            timeout_seconds=settings.rag_retrieval_timeout_seconds,
        )
    return await postgres_retrieve(
        kb_id=kb_id,
        user_id=user_id,
        query=query,
        session_id=session_id,
        doc_ids=doc_ids,
        temp_doc_ids=temp_doc_ids,
        top_k=top_k,
        search_mode=search_mode,
        ensure_document_coverage=ensure_document_coverage,
    )
