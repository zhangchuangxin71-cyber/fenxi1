from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from app.api.schemas import (
    DocumentMeta,
    DocumentMetaRequest,
    DocumentMetaResponse,
    DocumentRawRepairAccepted,
    DocumentRawRequest,
    DocumentRawResponse,
    DocumentRawStatusRequest,
    DocumentRawStatusResponse,
)
from app.core.errors import ApiError


def _is_complete_raw_mineru(value: Any) -> bool:
    """Match the ingestion service's persisted MinerU completeness contract."""
    return (
        isinstance(value, Mapping)
        and isinstance(value.get("md_content"), str)
        and isinstance(value.get("content_list"), list)
        and isinstance(value.get("middle_json"), Mapping)
    )


class DocumentService:
    def __init__(
        self,
        *,
        repository: Any,
        db_executor: Any | None = None,
        max_doc_ids: int = 100,
        repair_client: Any | None = None,
        api_prefix: str = "/rag/v1",
    ) -> None:
        self.repository = repository
        self.db_executor = db_executor
        self.max_doc_ids = max(1, int(max_doc_ids))
        self.repair_client = repair_client
        self.api_prefix = "/" + api_prefix.strip("/")

    async def meta(self, request: DocumentMetaRequest) -> DocumentMetaResponse:
        if len(request.doc_ids) + len(request.temp_doc_ids) > self.max_doc_ids:
            raise ApiError(
                422,
                "TOO_MANY_DOCUMENT_IDS",
                "document ID count exceeds the service maximum",
            )
        try:
            documents, missing = await self._run(
                self.repository.fetch_document_meta,
                user_id=request.user_id,
                kb_id=request.kb_id,
                doc_ids=request.doc_ids,
                temp_doc_ids=request.temp_doc_ids,
                session_id=request.session_id,
            )
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(
                503,
                "DB_QUERY_FAILED",
                "failed to fetch document metadata",
                retryable=True,
            ) from exc
        return DocumentMetaResponse(
            documents=[DocumentMeta.model_validate(document) for document in documents],
            missing_doc_ids=missing if request.include_missing else [],
        )

    async def raw(
        self, request: DocumentRawRequest
    ) -> DocumentRawResponse | DocumentRawRepairAccepted:
        document = await self._fetch_raw_document(request)
        raw_mineru = document.get("raw_mineru")
        if _is_complete_raw_mineru(raw_mineru):
            return self._build_raw_response(document)

        self._raise_if_repair_unavailable(document)
        if self.repair_client is None:
            raise ApiError(
                503,
                "INGESTION_SERVICE_UNAVAILABLE",
                "raw repair service is not configured",
                retryable=True,
            )
        try:
            repair = await self.repair_client.submit(
                user_id=str(document.get("_binding_user_id") or request.user_id),
                kb_id=str(document.get("_binding_kb_id") or request.kb_id),
                doc_id=request.doc_id,
            )
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(
                int(getattr(exc, "status_code", 503)),
                str(getattr(exc, "code", "INGESTION_SERVICE_UNAVAILABLE")),
                str(exc) or "failed to submit MinerU raw repair",
                retryable=bool(getattr(exc, "retryable", True)),
            ) from exc
        if repair.get("status") == "failed":
            retryable = bool(repair.get("retryable", False))
            raise ApiError(
                503 if retryable else 422,
                str(repair.get("error_code") or "RAW_MINERU_REPAIR_FAILED"),
                str(
                    repair.get("error_message")
                    or repair.get("message")
                    or "MinerU raw repair is unavailable"
                ),
                retryable=retryable,
                details={"next_retry_at": repair.get("next_retry_at")}
                if repair.get("next_retry_at")
                else None,
            )
        if repair.get("status") == "completed":
            completed = await self._fetch_raw_document(request)
            if not _is_complete_raw_mineru(completed.get("raw_mineru")):
                raise ApiError(
                    503,
                    "RAW_MINERU_REPAIR_STATE_INCONSISTENT",
                    "MinerU repair completed but its result is not yet available",
                    retryable=True,
                )
            return self._build_raw_response(completed)
        query = urlencode(
            {"user_id": request.user_id, "kb_id": request.kb_id, "doc_id": request.doc_id}
        )
        return DocumentRawRepairAccepted(
            doc_id=request.doc_id,
            repair_id=str(repair["repair_id"]) if repair.get("repair_id") else None,
            task_id=str(repair["task_id"]) if repair.get("task_id") else None,
            status=str(repair.get("status") or "queued"),
            status_url=f"{self.api_prefix}/documents/raw/status?{query}",
        )

    async def raw_status(self, request: DocumentRawStatusRequest) -> DocumentRawStatusResponse:
        try:
            state = await self._run(
                self.repository.fetch_document_raw_state,
                user_id=request.user_id,
                kb_id=request.kb_id,
                doc_id=request.doc_id,
            )
        except Exception as exc:
            raise ApiError(
                503, "DB_QUERY_FAILED", "failed to fetch raw repair status", retryable=True
            ) from exc
        if state is None:
            raise ApiError(404, "DOCUMENT_NOT_FOUND", "document was not found")
        if _is_complete_raw_mineru(state.get("raw_mineru")):
            return DocumentRawStatusResponse(doc_id=request.doc_id, status="completed")
        repair = state.get("raw_mineru_repair")
        if not isinstance(repair, Mapping):
            return DocumentRawStatusResponse(doc_id=request.doc_id, status="not_started")
        payload = {
            key: repair.get(key)
            for key in DocumentRawStatusResponse.model_fields
            if key != "schema_version"
        }
        payload["doc_id"] = request.doc_id
        payload["status"] = repair.get("status") or "not_started"
        payload["message"] = repair.get("message") or repair.get("error_message")
        return DocumentRawStatusResponse.model_validate(payload)

    async def _fetch_raw_document(self, request: DocumentRawRequest) -> dict[str, Any]:
        try:
            document = await self._run(
                self.repository.fetch_document_raw,
                user_id=request.user_id,
                kb_id=request.kb_id,
                doc_id=request.doc_id,
            )
        except Exception as exc:
            raise ApiError(
                503, "DB_QUERY_FAILED", "failed to fetch document raw result", retryable=True
            ) from exc
        if document is None:
            raise ApiError(404, "DOCUMENT_NOT_FOUND", "document was not found")
        return document

    @staticmethod
    def _build_raw_response(document: Mapping[str, Any]) -> DocumentRawResponse:
        public_document = {
            key: value
            for key, value in document.items()
            if key
            not in {
                "file_oss_key",
                "raw_mineru_repair",
                "_binding_user_id",
                "_binding_kb_id",
            }
        }
        return DocumentRawResponse.model_validate(public_document)

    @staticmethod
    def _raise_if_repair_unavailable(document: Mapping[str, Any]) -> None:
        if not str(document.get("file_oss_key") or "").strip():
            raise ApiError(
                422,
                "DOCUMENT_OSS_KEY_MISSING",
                "document has no OSS key; delete the legacy document with the ingestion "
                "delete API and re-ingest it",
            )
        repair = document.get("raw_mineru_repair")
        if (
            isinstance(repair, Mapping)
            and repair.get("status") == "failed"
            and not repair.get("retryable", False)
        ):
            raise ApiError(
                422,
                str(repair.get("error_code") or "RAW_MINERU_REPAIR_FAILED"),
                str(
                    repair.get("message")
                    or repair.get("error_message")
                    or "MinerU cannot parse this document"
                ),
            )

    async def _run(self, function: Any, **kwargs: Any) -> Any:
        if self.db_executor is None:
            return function(**kwargs)
        return await self.db_executor.run(function, **kwargs)
