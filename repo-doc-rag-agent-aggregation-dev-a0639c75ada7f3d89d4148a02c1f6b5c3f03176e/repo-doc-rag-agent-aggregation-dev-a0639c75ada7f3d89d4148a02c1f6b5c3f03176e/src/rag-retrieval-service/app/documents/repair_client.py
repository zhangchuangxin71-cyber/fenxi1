from __future__ import annotations

from typing import Any

import httpx


class RawRepairClientError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 503,
        code: str = "INGESTION_SERVICE_UNAVAILABLE",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


class RawRepairClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds
        )

    async def submit(self, *, user_id: str, kb_id: str, doc_id: str) -> dict[str, Any]:
        try:
            response = await self._client.post(
                "/ingestion/v1/internal/documents/raw/repair",
                json={"user_id": user_id, "kb_id": kb_id, "doc_id": doc_id},
            )
        except httpx.RequestError as exc:
            raise RawRepairClientError("ingestion service is unavailable") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise RawRepairClientError("ingestion service returned an invalid response") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if response.is_error:
            error = data if isinstance(data, dict) else {}
            raise RawRepairClientError(
                str(payload.get("message") or "raw repair request failed"),
                status_code=response.status_code,
                code=str(error.get("error_code") or "RAW_MINERU_REPAIR_FAILED"),
                retryable=bool(error.get("retryable", response.status_code >= 500)),
            )
        if not isinstance(data, dict) or (
            data.get("status") != "completed" and not data.get("repair_id")
        ):
            raise RawRepairClientError("ingestion service returned an invalid repair payload")
        return data

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
