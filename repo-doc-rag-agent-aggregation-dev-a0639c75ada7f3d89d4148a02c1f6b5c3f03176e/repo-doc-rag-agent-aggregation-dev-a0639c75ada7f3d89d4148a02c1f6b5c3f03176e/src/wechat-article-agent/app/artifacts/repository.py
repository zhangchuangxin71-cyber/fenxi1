from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.artifacts.models import ApprovedStage, ArticleArtifact, RevisionCreate
from app.core.errors import AppError
from app.core.ids import advisory_lock_key
from app.persistence.retry import TRANSIENT_DATABASE_ERRORS, retry_database_operation

_STAGE_ORDER: dict[str, int] = {
    "none": 0,
    "docs_research": 1,
    "task_spec": 2,
    "outline": 3,
    "article": 4,
}
_ENTRY_ORDER: dict[str, int] = {
    "docs_research": 1,
    "task_spec": 2,
    "outline": 3,
    "article": 4,
}


class ArtifactRepository:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        ttl_hours: int = 72,
        max_retries: int = 2,
    ) -> None:
        self.pool = pool
        self.ttl = timedelta(hours=ttl_hours)
        self.max_retries = max_retries

    @asynccontextmanager
    async def session_lock(self, thread_id: str) -> AsyncIterator[AsyncConnection[Any]]:
        for attempt in range(self.max_retries + 1):
            yielded = False
            try:
                async with self.pool.connection() as connection:
                    async with connection.transaction():
                        await connection.execute(
                            "SELECT pg_advisory_xact_lock(%s)", (advisory_lock_key(thread_id),)
                        )
                        yielded = True
                        yield connection
                return
            except TRANSIENT_DATABASE_ERRORS as exc:
                # Never replay a transaction after control returned to the caller: the
                # caller may already have submitted an Agent Server run.
                if yielded or attempt >= self.max_retries:
                    raise AppError(
                        503,
                        "DATABASE_UNAVAILABLE",
                        "The article database is temporarily unavailable.",
                        True,
                    ) from exc
                await asyncio.sleep(0.1 * (2**attempt))

    async def latest(
        self, session_id: str, *, connection: AsyncConnection[Any] | None = None
    ) -> ArticleArtifact | None:
        query = "SELECT * FROM article_artifacts WHERE session_id = %s ORDER BY revision DESC LIMIT 1"
        if connection is not None:
            cursor = await connection.execute(query, (session_id,))
            row = await cursor.fetchone()
        else:

            async def execute() -> Mapping[str, Any] | None:
                async with self.pool.connection() as owned:
                    cursor = await owned.execute(query, (session_id,))
                    return cast(Mapping[str, Any] | None, await cursor.fetchone())

            row = await retry_database_operation(execute, max_retries=self.max_retries)
        return self._model(row)

    async def get(
        self, artifact_id: str, *, connection: AsyncConnection[Any] | None = None
    ) -> ArticleArtifact | None:
        query = "SELECT * FROM article_artifacts WHERE artifact_id = %s"
        if connection is not None:
            cursor = await connection.execute(query, (artifact_id,))
            row = await cursor.fetchone()
        else:

            async def execute() -> Mapping[str, Any] | None:
                async with self.pool.connection() as owned:
                    cursor = await owned.execute(query, (artifact_id,))
                    return cast(Mapping[str, Any] | None, await cursor.fetchone())

            row = await retry_database_operation(execute, max_retries=self.max_retries)
        return self._model(row)

    async def nearest_approved_ancestor(
        self,
        artifact: ArticleArtifact,
        *,
        connection: AsyncConnection[Any] | None = None,
    ) -> ArticleArtifact | None:
        parent_id = artifact.parent_artifact_id
        visited: set[str] = set()
        while parent_id:
            if parent_id in visited:
                raise AppError(409, "ARTIFACT_CHAIN_INVALID", "Artifact revision chain contains a cycle.")
            visited.add(parent_id)
            parent = await self.get(parent_id, connection=connection)
            if parent is None:
                raise AppError(409, "ARTIFACT_CHAIN_INVALID", "Artifact revision parent is missing.")
            if parent.approved_through_stage != "none":
                return parent
            parent_id = parent.parent_artifact_id
        return None

    async def create_revision(
        self,
        create: RevisionCreate,
        *,
        connection: AsyncConnection[Any] | None = None,
    ) -> ArticleArtifact:
        if connection is None:
            return await self._transaction(lambda owned: self.create_revision(create, connection=owned))

        previous = await self.latest(create.session_id, connection=connection)
        revision = 1 if previous is None else previous.revision + 1
        inherited = self._inherited_fields(previous, create.entry_stage)
        expires_at = datetime.now(UTC) + self.ttl
        cursor = await connection.execute(
            """
            INSERT INTO article_artifacts (
                artifact_id, session_id, run_id, current_response_id, revision,
                parent_artifact_id, status, current_stage, approved_through_stage,
                doc_ids, temp_doc_ids, relevant_doc_ids, document_coverage_mode,
                research_direction, material_library, task_spec, outline,
                article_markdown, images, final_html,
                expires_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, 'running', %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) RETURNING *
            """,
            (
                create.artifact_id,
                create.session_id,
                create.run_id,
                create.current_response_id,
                revision,
                previous.artifact_id if previous else create.parent_artifact_id,
                create.entry_stage,
                inherited["approved_through_stage"],
                Jsonb(create.doc_ids),
                Jsonb(create.temp_doc_ids),
                Jsonb(inherited["relevant_doc_ids"]),
                inherited.get("document_coverage_mode", create.document_coverage_mode),
                Jsonb(inherited["research_direction"])
                if inherited["research_direction"] is not None
                else None,
                Jsonb(inherited["material_library"]) if inherited["material_library"] is not None else None,
                Jsonb(inherited["task_spec"]) if inherited["task_spec"] is not None else None,
                Jsonb(inherited["outline"]) if inherited["outline"] is not None else None,
                inherited["article_markdown"],
                Jsonb(inherited["images"]) if inherited["images"] is not None else None,
                inherited["final_html"],
                expires_at,
            ),
        )
        row = await cursor.fetchone()
        assert row is not None
        return ArticleArtifact.model_validate(row)

    async def set_runtime_run(
        self,
        artifact_id: str,
        *,
        expected_response_id: str,
        response_id: str,
        runtime_run_id: str,
        connection: AsyncConnection[Any] | None = None,
    ) -> ArticleArtifact:
        return await self._conditional_update(
            artifact_id,
            expected_response_id=expected_response_id,
            allowed_statuses=("running", "waiting_for_input"),
            values={
                "current_response_id": response_id,
                "active_runtime_run_id": runtime_run_id,
                "status": "running",
                "last_error": None,
            },
            connection=connection,
        )

    async def retry_failed(
        self,
        artifact_id: str,
        *,
        expected_response_id: str,
        response_id: str,
        runtime_run_id: str,
        connection: AsyncConnection[Any] | None = None,
    ) -> ArticleArtifact:
        return await self._conditional_update(
            artifact_id,
            expected_response_id=expected_response_id,
            allowed_statuses=("failed",),
            values={
                "current_response_id": response_id,
                "active_runtime_run_id": runtime_run_id,
                "status": "running",
                "last_error": None,
            },
            connection=connection,
        )

    async def initialize_entry_stage(
        self,
        artifact_id: str,
        *,
        response_id: str,
        entry_stage: Literal["docs_research", "task_spec", "outline", "article"],
        connection: AsyncConnection[Any] | None = None,
    ) -> ArticleArtifact:
        if connection is None:
            async with self.pool.connection() as owned:
                async with owned.transaction():
                    return await self.initialize_entry_stage(
                        artifact_id,
                        response_id=response_id,
                        entry_stage=entry_stage,
                        connection=owned,
                    )
        current = await self.get(artifact_id, connection=connection)
        if current is None:
            raise AppError(404, "ARTIFACT_NOT_FOUND", "The article artifact does not exist.")
        parent = await self.nearest_approved_ancestor(current, connection=connection)
        inherited = self._inherited_fields(parent, entry_stage)
        values = {
            **inherited,
            "current_stage": entry_stage,
            "last_error": None,
        }
        return await self._conditional_update(
            artifact_id,
            expected_response_id=response_id,
            allowed_statuses=("running",),
            values=values,
            connection=connection,
        )

    async def update_stage(
        self,
        artifact_id: str,
        *,
        response_id: str,
        current_stage: str,
        values: Mapping[str, Any] | None = None,
        status: Literal["running", "waiting_for_input", "completed", "failed"] = "running",
        connection: AsyncConnection[Any] | None = None,
    ) -> ArticleArtifact:
        allowed_fields = {
            "relevant_doc_ids",
            "document_coverage_mode",
            "research_direction",
            "material_library",
            "task_spec",
            "outline",
            "article_markdown",
            "images",
            "final_html",
            "last_error",
        }
        update_values = dict(values or {})
        unknown = set(update_values) - allowed_fields
        if unknown:
            raise ValueError(f"unsupported artifact fields: {sorted(unknown)}")
        update_values.update(
            {
                "current_stage": current_stage,
                "status": status,
                "active_runtime_run_id": None if status != "running" else _UNCHANGED,
            }
        )
        return await self._conditional_update(
            artifact_id,
            expected_response_id=response_id,
            allowed_statuses=("running", "waiting_for_input"),
            values=update_values,
            connection=connection,
        )

    async def replace_materials(
        self,
        artifact_id: str,
        *,
        response_id: str,
        materials: list[dict[str, Any]],
        sources: set[str] | None = None,
        kinds: set[str] | None = None,
        connection: AsyncConnection[Any] | None = None,
    ) -> ArticleArtifact:
        """Atomically replace one worker-owned subset of the material snapshot."""

        if connection is None:
            return await self._transaction(
                lambda owned: self.replace_materials(
                    artifact_id,
                    response_id=response_id,
                    materials=materials,
                    sources=sources,
                    kinds=kinds,
                    connection=owned,
                )
            )
        cursor = await connection.execute(
            "SELECT * FROM article_artifacts WHERE artifact_id=%s FOR UPDATE",
            (artifact_id,),
        )
        row = await cursor.fetchone()
        current = self._model(row)
        if current is None:
            raise AppError(404, "ARTIFACT_NOT_FOUND", "The article artifact does not exist.")
        if current.current_response_id != response_id:
            raise AppError(409, "STALE_RESPONSE", "The response no longer controls this workflow.")
        if current.status != "running":
            raise AppError(409, "STATE_ARTIFACT_MISMATCH", "Artifact state changed concurrently.")
        retained = []
        for item in current.material_library or []:
            item_source = str(item.get("source") or item.get("source_doc_id") or "")
            source_matches = sources is not None and item_source in sources
            kind_matches = kinds is not None and str(item.get("material_kind") or "user_document") in kinds
            if not source_matches and not kind_matches:
                retained.append(item)
        return await self._conditional_update(
            artifact_id,
            expected_response_id=response_id,
            allowed_statuses=("running",),
            values={"material_library": [*retained, *materials]},
            connection=connection,
        )

    async def mark_approved(
        self,
        artifact_id: str,
        *,
        response_id: str,
        stage: ApprovedStage,
        next_stage: str,
        connection: AsyncConnection[Any] | None = None,
    ) -> ArticleArtifact:
        if stage == "none":
            raise ValueError("none cannot be approved")
        return await self._conditional_update(
            artifact_id,
            expected_response_id=response_id,
            allowed_statuses=("running",),
            values={"approved_through_stage": stage, "current_stage": next_stage},
            extra_predicate=sql.SQL("AND approved_through_stage = ANY(%s)"),
            extra_params=([name for name, order in _STAGE_ORDER.items() if order < _STAGE_ORDER[stage]],),
            connection=connection,
        )

    async def begin_cancel(
        self,
        artifact_id: str,
        *,
        response_id: str,
        connection: AsyncConnection[Any] | None = None,
    ) -> ArticleArtifact:
        return await self._conditional_update(
            artifact_id,
            expected_response_id=response_id,
            allowed_statuses=("running", "waiting_for_input", "cancelling"),
            values={"status": "cancelling", "current_stage": "cancelling"},
            connection=connection,
        )

    async def set_cancelling_runtime(
        self,
        artifact_id: str,
        *,
        response_id: str,
        runtime_run_id: str,
        connection: AsyncConnection[Any] | None = None,
    ) -> ArticleArtifact:
        return await self._conditional_update(
            artifact_id,
            expected_response_id=response_id,
            allowed_statuses=("cancelling",),
            values={"active_runtime_run_id": runtime_run_id},
            connection=connection,
        )

    async def finish_cancel(
        self,
        artifact_id: str,
        *,
        response_id: str,
        connection: AsyncConnection[Any] | None = None,
    ) -> ArticleArtifact:
        return await self._conditional_update(
            artifact_id,
            expected_response_id=response_id,
            allowed_statuses=("cancelling",),
            values={"status": "cancelled", "current_stage": "cancelled", "active_runtime_run_id": None},
            connection=connection,
        )

    async def mark_terminal(
        self,
        artifact_id: str,
        *,
        response_id: str,
        status: Literal["completed", "failed", "cancelled", "superseded"],
        current_stage: str,
        last_error: Mapping[str, Any] | None = None,
        connection: AsyncConnection[Any] | None = None,
    ) -> ArticleArtifact:
        allowed = ("cancelling",) if status == "cancelled" else ("running", "waiting_for_input")
        return await self._conditional_update(
            artifact_id,
            expected_response_id=response_id,
            allowed_statuses=allowed,
            values={
                "status": status,
                "current_stage": current_stage,
                "last_error": dict(last_error) if last_error else None,
                "active_runtime_run_id": None,
            },
            connection=connection,
        )

    async def count_running(self) -> int:
        async def execute() -> int:
            async with self.pool.connection() as connection:
                cursor = await connection.execute(
                    "SELECT count(*) AS count FROM article_artifacts WHERE status='running'"
                )
                row = await cursor.fetchone()
                return int(cast(Mapping[str, Any], row)["count"] if row else 0)

        return await retry_database_operation(execute, max_retries=self.max_retries)

    async def refresh_ttl(self, artifact_id: str) -> None:
        async def execute() -> None:
            async with self.pool.connection() as connection:
                async with connection.transaction():
                    await connection.execute(
                        "UPDATE article_artifacts SET expires_at=%s, updated_at=now() WHERE artifact_id=%s",
                        (datetime.now(UTC) + self.ttl, artifact_id),
                    )

        await retry_database_operation(execute, max_retries=self.max_retries)

    async def list_cancelling(self, *, limit: int = 20) -> list[ArticleArtifact]:
        async def execute() -> list[ArticleArtifact]:
            async with self.pool.connection() as connection, connection.transaction():
                cursor = await connection.execute(
                    """
                    SELECT * FROM article_artifacts
                    WHERE status='cancelling'
                    ORDER BY updated_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                    """,
                    (limit,),
                )
                return [ArticleArtifact.model_validate(row) for row in await cursor.fetchall()]

        return await retry_database_operation(execute, max_retries=self.max_retries)

    async def expired_sessions(self, *, limit: int = 50) -> list[str]:
        async def execute() -> list[str]:
            async with self.pool.connection() as connection:
                cursor = await connection.execute(
                    """
                    SELECT session_id
                    FROM article_artifacts
                    GROUP BY session_id
                    HAVING max(expires_at) < now()
                    ORDER BY max(expires_at)
                    LIMIT %s
                    """,
                    (limit,),
                )
                return [
                    cast(str, cast(Mapping[str, Any], row)["session_id"]) for row in await cursor.fetchall()
                ]

        return await retry_database_operation(execute, max_retries=self.max_retries)

    async def delete_session(self, session_id: str, *, connection: AsyncConnection[Any] | None = None) -> int:
        if connection is None:
            return await self._transaction(lambda owned: self.delete_session(session_id, connection=owned))
        await connection.execute(
            "UPDATE article_artifacts SET parent_artifact_id=NULL WHERE session_id=%s",
            (session_id,),
        )
        cursor = await connection.execute(
            "DELETE FROM article_artifacts WHERE session_id=%s",
            (session_id,),
        )
        return cursor.rowcount

    async def _conditional_update(
        self,
        artifact_id: str,
        *,
        expected_response_id: str,
        allowed_statuses: tuple[str, ...],
        values: Mapping[str, Any],
        extra_predicate: sql.SQL | None = None,
        extra_params: tuple[Any, ...] = (),
        connection: AsyncConnection[Any] | None = None,
    ) -> ArticleArtifact:
        if connection is None:
            return await self._transaction(
                lambda owned: self._conditional_update(
                    artifact_id,
                    expected_response_id=expected_response_id,
                    allowed_statuses=allowed_statuses,
                    values=values,
                    extra_predicate=extra_predicate,
                    extra_params=extra_params,
                    connection=owned,
                )
            )
        assignments: list[sql.Composable] = []
        params: list[Any] = []
        for name, value in values.items():
            if value is _UNCHANGED:
                continue
            assignments.append(sql.SQL("{} = %s").format(sql.Identifier(name)))
            params.append(Jsonb(value) if isinstance(value, (dict, list)) else value)
        assignments.extend([sql.SQL("updated_at = now()"), sql.SQL("expires_at = %s")])
        params.append(datetime.now(UTC) + self.ttl)
        query = sql.SQL(
            "UPDATE article_artifacts SET {} "
            "WHERE artifact_id=%s AND current_response_id=%s AND status = ANY(%s) {} RETURNING *"
        ).format(sql.SQL(", ").join(assignments), extra_predicate or sql.SQL(""))
        params.extend([artifact_id, expected_response_id, list(allowed_statuses), *extra_params])
        cursor = await connection.execute(query, tuple(params))
        row = await cursor.fetchone()
        if row is None:
            current = await self.get(artifact_id, connection=connection)
            if current is None:
                raise AppError(404, "ARTIFACT_NOT_FOUND", "The article artifact does not exist.")
            if current.current_response_id != expected_response_id:
                raise AppError(409, "STALE_RESPONSE", "The response no longer controls this workflow.")
            if current.status in {"cancelling", "cancelled"}:
                raise AppError(409, "RUN_CANCELLED", "The workflow is cancelling or cancelled.")
            raise AppError(409, "STATE_ARTIFACT_MISMATCH", "Artifact state changed concurrently.")
        return ArticleArtifact.model_validate(row)

    async def _transaction[ResultT](
        self, operation: Callable[[AsyncConnection[Any]], Awaitable[ResultT]]
    ) -> ResultT:
        async def execute() -> ResultT:
            async with self.pool.connection() as owned, owned.transaction():
                return await operation(owned)

        return await retry_database_operation(execute, max_retries=self.max_retries)

    @staticmethod
    def _model(row: Mapping[str, Any] | None) -> ArticleArtifact | None:
        return ArticleArtifact.model_validate(row) if row else None

    @staticmethod
    def _inherited_fields(previous: ArticleArtifact | None, entry_stage: str) -> dict[str, Any]:
        empty: dict[str, Any] = {
            "approved_through_stage": "none",
            "relevant_doc_ids": [],
            "document_coverage_mode": "best_effort",
            "research_direction": None,
            "material_library": None,
            "task_spec": None,
            "outline": None,
            "article_markdown": None,
            "images": None,
            "final_html": None,
        }
        if previous is None or entry_stage == "docs_research":
            return empty
        approved_order = _STAGE_ORDER[previous.approved_through_stage]
        entry_order = _ENTRY_ORDER[entry_stage]
        inherited_order = min(approved_order, entry_order - 1)
        result: dict[str, Any] = dict(empty)
        if inherited_order >= 1:
            result.update(
                {
                    "approved_through_stage": "docs_research",
                    "relevant_doc_ids": previous.relevant_doc_ids,
                    "document_coverage_mode": previous.document_coverage_mode,
                    "research_direction": previous.research_direction,
                    "material_library": previous.material_library,
                }
            )
        if inherited_order >= 2:
            result.update({"approved_through_stage": "task_spec", "task_spec": previous.task_spec})
        if inherited_order >= 3:
            result.update({"approved_through_stage": "outline", "outline": previous.outline})
        if inherited_order >= 4:
            result.update(
                {
                    "approved_through_stage": "article",
                    "article_markdown": previous.article_markdown,
                }
            )
        return result


_UNCHANGED = object()
