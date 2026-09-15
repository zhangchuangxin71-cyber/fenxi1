"""Workspace TTL purge: classify stale workspaces and optionally delete them."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from videoaudiotext.api.job_context import is_managed_job_directory, jobs_root, load_job_store
from videoaudiotext.api.workspace.store import WorkspaceStore

WorkspaceState = Literal["empty", "in_progress", "composed"]
SkipReason = Literal[
    "pinned",
    "active_job",
    "within_grace",
    "not_expired",
    "state_excluded",
    "max_delete_reached",
]

OUTPUT_MIN_BYTES = 1024


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass
class PurgePolicy:
    ttl_empty_days: float = field(
        default_factory=lambda: _env_float("WORKSPACE_TTL_EMPTY_DAYS", 3.0)
    )
    ttl_abandoned_days: float = field(
        default_factory=lambda: _env_float("WORKSPACE_TTL_ABANDONED_DAYS", 7.0)
    )
    ttl_composed_days: float = field(
        default_factory=lambda: _env_float("WORKSPACE_TTL_COMPOSED_DAYS", 30.0)
    )
    grace_hours: float = field(
        default_factory=lambda: _env_float("WORKSPACE_GRACE_HOURS", 24.0)
    )

    def as_dict(self) -> dict[str, float]:
        return {
            "ttl_empty_days": self.ttl_empty_days,
            "ttl_abandoned_days": self.ttl_abandoned_days,
            "ttl_composed_days": self.ttl_composed_days,
            "grace_hours": self.grace_hours,
        }


def workspace_tree_bytes(root: Path) -> int:
    total = 0
    if not root.is_dir():
        return 0
    for p in root.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def workspace_last_activity_at(store: WorkspaceStore) -> datetime:
    meta = store.read_meta()
    for key in ("last_activity_at", "compose_completed_at", "created_at"):
        dt = _parse_iso(meta.get(key))
        if dt:
            return dt
    if store.created_at:
        dt = _parse_iso(store.created_at)
        if dt:
            return dt
    try:
        return datetime.fromtimestamp(store.root.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return _now()


def classify_workspace(store: WorkspaceStore) -> WorkspaceState:
    store.refresh_flags()
    output = store.output_mp4
    if output.is_file() and output.stat().st_size >= OUTPUT_MIN_BYTES:
        return "composed"
    if store.flags.split_ready or store.flags.audio_ready or store.flags.media_bound:
        return "in_progress"
    if (store.config_dir / "split_plan.json").is_file():
        return "in_progress"
    return "empty"


def _ttl_days_for_state(state: WorkspaceState, policy: PurgePolicy) -> float:
    if state == "composed":
        return policy.ttl_composed_days
    if state == "in_progress":
        return policy.ttl_abandoned_days
    return policy.ttl_empty_days


def evaluate_workspace(
    store: WorkspaceStore,
    policy: PurgePolicy,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or _now()
    state = classify_workspace(store)
    last_activity = workspace_last_activity_at(store)
    created_raw = store.read_meta().get("created_at") or store.created_at
    created_at = _parse_iso(created_raw) or last_activity
    age = now - last_activity
    grace = timedelta(hours=policy.grace_hours)
    ttl = timedelta(days=_ttl_days_for_state(state, policy))

    eligible = False
    skip_reason: SkipReason | None = None
    purge_reason: str | None = None

    if store.is_pinned():
        skip_reason = "pinned"
    elif store.active_compose_job_id:
        skip_reason = "active_job"
    elif now - created_at < grace:
        skip_reason = "within_grace"
    elif age < ttl:
        skip_reason = "not_expired"
    else:
        eligible = True
        if state == "composed":
            purge_reason = "composed_ttl_expired"
        elif state == "in_progress":
            purge_reason = "abandoned_ttl_expired"
        else:
            purge_reason = "empty_ttl_expired"

    return {
        "workspace_id": store.workspace_id,
        "label": store.label,
        "state": state,
        "last_activity_at": last_activity.isoformat(),
        "age_hours": round(age.total_seconds() / 3600, 2),
        "size_bytes": workspace_tree_bytes(store.root),
        "eligible": eligible,
        "skip_reason": skip_reason,
        "reason": purge_reason,
        "pinned": store.is_pinned(),
        "active_compose_job_id": store.active_compose_job_id,
    }


def purge_workspaces(
    *,
    dry_run: bool = True,
    max_delete: int = 100,
    policy: PurgePolicy | None = None,
    include_states: frozenset[WorkspaceState] | None = None,
    include_all_evaluated: bool = True,
) -> dict[str, Any]:
    policy = policy or PurgePolicy()
    root = jobs_root()
    scanned = 0
    eligible_count = 0
    deleted: list[str] = []
    candidates: list[dict[str, Any]] = []
    skipped: dict[str, int] = {
        "pinned": 0,
        "active_job": 0,
        "within_grace": 0,
        "not_expired": 0,
        "state_excluded": 0,
        "max_delete_reached": 0,
    }

    if not root.is_dir():
        return _purge_result(
            dry_run=dry_run,
            policy=policy,
            scanned=0,
            eligible_count=0,
            deleted=deleted,
            skipped=skipped,
            candidates=candidates,
            rows=[] if include_all_evaluated else None,
        )

    rows: list[dict[str, Any]] = []
    for ws_dir in sorted(root.iterdir()):
        if not ws_dir.is_dir() or not is_managed_job_directory(ws_dir.name):
            continue
        store = load_job_store(ws_dir.name)
        if not store:
            continue
        scanned += 1
        row = evaluate_workspace(store, policy)
        if row["eligible"] and include_states is not None:
            if row["state"] not in include_states:
                row = {**row, "eligible": False, "skip_reason": "state_excluded"}
        rows.append(row)
        if row["eligible"]:
            eligible_count += 1
            candidates.append(row)
        elif row["skip_reason"]:
            key = str(row["skip_reason"])
            skipped[key] = skipped.get(key, 0) + 1

    candidates.sort(key=lambda r: r["last_activity_at"])
    remaining_cap = max(0, max_delete)
    for row in candidates:
        if remaining_cap <= 0:
            skipped["max_delete_reached"] += 1
            continue
        ws_id = row["workspace_id"]
        if dry_run:
            deleted.append(ws_id)
            remaining_cap -= 1
            continue
        store = load_job_store(ws_id)
        if store:
            store.delete_tree()
            deleted.append(ws_id)
            remaining_cap -= 1

    return _purge_result(
        dry_run=dry_run,
        policy=policy,
        scanned=scanned,
        eligible_count=eligible_count,
        deleted=deleted,
        skipped=skipped,
        candidates=candidates,
        rows=rows if include_all_evaluated else None,
    )


def _purge_result(
    *,
    dry_run: bool,
    policy: PurgePolicy,
    scanned: int,
    eligible_count: int,
    deleted: list[str],
    skipped: dict[str, int],
    candidates: list[dict[str, Any]],
    rows: list[dict[str, Any]] | None,
    mode: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "dry_run": dry_run,
        "policy": policy.as_dict(),
        "scanned": scanned,
        "eligible": eligible_count,
        "deleted": len(deleted),
        "deleted_ids": deleted,
        "skipped": skipped,
        "candidates": candidates[: max(len(deleted) or 0, 50)],
    }
    if mode:
        payload["mode"] = mode
    if rows is not None:
        payload["all_evaluated"] = rows
    return payload


def purge_workspaces_on_startup() -> dict[str, Any]:
    """Conservative startup purge: empty + abandoned only (never composed)."""
    if not _env_bool("WORKSPACE_STARTUP_PURGE", True):
        return {"enabled": False, "deleted": 0, "deleted_ids": []}
    max_delete = int(_env_float("WORKSPACE_STARTUP_PURGE_MAX", 20))
    result = purge_workspaces(
        dry_run=False,
        max_delete=max(0, max_delete),
        include_states=frozenset({"empty", "in_progress"}),
        include_all_evaluated=False,
    )
    result["enabled"] = True
    result["mode"] = "startup_conservative"
    return result
