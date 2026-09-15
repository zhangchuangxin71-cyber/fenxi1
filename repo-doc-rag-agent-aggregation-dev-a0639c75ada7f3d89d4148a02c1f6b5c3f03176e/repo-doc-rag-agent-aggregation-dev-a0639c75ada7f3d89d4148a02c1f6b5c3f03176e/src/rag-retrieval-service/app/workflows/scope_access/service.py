from __future__ import annotations

from app.api.schemas import RetrievalWarning
from app.core.models import ToolOutput
from app.db.repositories import DocumentProfile
from app.tools.metadata import build_scope_descriptions, build_scope_metainfo
from app.workflows.classification.models import QueryGroup
from app.workflows.scope_access.models import ScopePlanError
from app.workflows.scope_access.planner import LLMScopePlanner, RuleScopePlanner


class ScopeAccessService:
    def __init__(
        self,
        *,
        primary: LLMScopePlanner | None = None,
        fallback: RuleScopePlanner | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback or RuleScopePlanner()

    async def run(
        self,
        *,
        group: QueryGroup,
        profiles: list[DocumentProfile],
        request_id: str = "",
        original_query: str = "",
    ) -> ToolOutput:
        query = "；".join(group.queries)
        original_query = original_query or query
        warnings: list[RetrievalWarning] = []
        try:
            if self.primary is None:
                raise ScopePlanError("LLM scope planner is not configured")
            plan = await self.primary.plan(
                request_id=request_id,
                query=query,
                original_query=original_query,
                scope_count=len(profiles),
            )
        except Exception:
            try:
                plan = self.fallback.plan(query=query, original_query=original_query)
                if self.primary is not None:
                    warnings.append(
                        RetrievalWarning(
                            code="SCOPE_TOOL_PLANNER_RULE_FALLBACK",
                            message="scope tool planning used deterministic rules",
                            affected_group_refs=[group.group_ref],
                            retryable=True,
                        )
                    )
            except ScopePlanError:
                return ToolOutput(
                    chunks=[],
                    warnings=[
                        RetrievalWarning(
                            code="SCOPE_TOOL_PLANNER_FAILED",
                            message="scope request could not be resolved to a safe tool call",
                            affected_group_refs=[group.group_ref],
                            retryable=True,
                        )
                    ],
                    coverage_complete=False,
                )
        if plan.tool_name == "get_docs_description":
            output = build_scope_descriptions(
                profiles=profiles, group_ref=group.group_ref, questions=group.queries
            )
        else:
            output = build_scope_metainfo(
                profiles=profiles,
                group_ref=group.group_ref,
                questions=group.queries,
                fields=plan.metainfo_fields or ["doc_name"],
                enumeration_limit=plan.enumeration_limit,
            )
        return output.model_copy(update={"warnings": [*warnings, *output.warnings]})
