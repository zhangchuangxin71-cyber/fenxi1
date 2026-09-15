"""Audio revision storage: up to MAX_REVISIONS draft/active copies per workspace."""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from videoaudiotext.api.errors import bad_request, conflict, not_found
from videoaudiotext.api.workspace.store import WorkspaceStore

MAX_AUDIO_REVISIONS = 10


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_revision_id() -> str:
    return f"rev_{uuid.uuid4().hex[:12]}"


def revisions_index_path(store: WorkspaceStore) -> Path:
    return store.config_dir / "audio_revisions.json"


def revision_audio_dir(store: WorkspaceStore, revision_id: str) -> Path:
    return store.audio_dir / "revisions" / revision_id


def revision_subtitles_dir(store: WorkspaceStore, revision_id: str) -> Path:
    return store.subtitles_dir / "revisions" / revision_id


def revision_meta_path(store: WorkspaceStore, revision_id: str) -> Path:
    return revision_audio_dir(store, revision_id) / "meta.json"


def load_revisions_index(store: WorkspaceStore) -> dict[str, Any]:
    data = store.read_json(revisions_index_path(store))
    if not data:
        return {
            "max_revisions": MAX_AUDIO_REVISIONS,
            "active_revision_id": None,
            "revisions": [],
        }
    data.setdefault("max_revisions", MAX_AUDIO_REVISIONS)
    data.setdefault("active_revision_id", None)
    data.setdefault("revisions", [])
    return data


def save_revisions_index(store: WorkspaceStore, data: dict[str, Any]) -> None:
    store.write_json(revisions_index_path(store), data)


def _find_revision_entry(index: dict[str, Any], revision_id: str) -> dict[str, Any] | None:
    for item in index.get("revisions") or []:
        if item.get("revision_id") == revision_id:
            return item
    return None


def find_revision_by_digest(store: WorkspaceStore, digest: str) -> dict[str, Any] | None:
    if not digest:
        return None
    index = load_revisions_index(store)
    for item in index.get("revisions") or []:
        if item.get("audio_config_digest") == digest:
            return item
    return None


def get_revision_entry(store: WorkspaceStore, revision_id: str) -> dict[str, Any]:
    index = load_revisions_index(store)
    entry = _find_revision_entry(index, revision_id)
    if not entry:
        raise not_found("revision not found", revision_id=revision_id)
    if not revision_meta_path(store, revision_id).is_file():
        raise not_found("revision not found", revision_id=revision_id)
    return entry


def revision_summary(store: WorkspaceStore, entry: dict[str, Any]) -> dict[str, Any]:
    rev_id = str(entry["revision_id"])
    index = load_revisions_index(store)
    return {
        "revision_id": rev_id,
        "label": entry.get("label") or "",
        "status": entry.get("status") or "draft",
        "created_at": entry.get("created_at"),
        "audio_config_digest": entry.get("audio_config_digest") or "",
        "total_speech_sec": entry.get("total_speech_sec"),
        "total_with_gaps_sec": entry.get("total_with_gaps_sec"),
        "segment_count": entry.get("segment_count"),
        "is_active": rev_id == index.get("active_revision_id"),
    }


def list_audio_revisions(store: WorkspaceStore) -> dict[str, Any]:
    index = load_revisions_index(store)
    entries = list(index.get("revisions") or [])
    active_id = index.get("active_revision_id")

    def sort_key(item: dict[str, Any]) -> tuple[int, str]:
        is_active = 1 if item.get("revision_id") == active_id else 0
        return (-is_active, str(item.get("created_at") or ""))

    entries.sort(key=sort_key, reverse=True)
    return {
        "active_revision_id": active_id,
        "max_revisions": int(index.get("max_revisions") or MAX_AUDIO_REVISIONS),
        "count": len(entries),
        "revisions": [revision_summary(store, e) for e in entries],
    }


def _delete_revision_tree(store: WorkspaceStore, revision_id: str) -> None:
    for root in (
        revision_audio_dir(store, revision_id),
        revision_subtitles_dir(store, revision_id),
    ):
        if root.is_dir():
            shutil.rmtree(root, ignore_errors=True)


def _evict_revisions(store: WorkspaceStore, index: dict[str, Any]) -> list[str]:
    max_n = int(index.get("max_revisions") or MAX_AUDIO_REVISIONS)
    evicted: list[str] = []
    revisions: list[dict[str, Any]] = list(index.get("revisions") or [])
    active_id = index.get("active_revision_id")

    while len(revisions) >= max_n:
        candidates = [r for r in revisions if r.get("revision_id") != active_id]
        if not candidates:
            break
        candidates.sort(key=lambda r: str(r.get("created_at") or ""))
        victim = candidates[0]
        vid = str(victim["revision_id"])
        _delete_revision_tree(store, vid)
        revisions = [r for r in revisions if r.get("revision_id") != vid]
        evicted.append(vid)

    index["revisions"] = revisions
    return evicted


def register_revision(
    store: WorkspaceStore,
    *,
    revision_id: str,
    label: str,
    audio_config_digest: str,
    segment_count: int,
    total_speech_sec: float,
    total_with_gaps_sec: float,
) -> list[str]:
    store.ensure_tree()
    index = load_revisions_index(store)
    entry = {
        "revision_id": revision_id,
        "label": label,
        "status": "draft",
        "created_at": _now_iso(),
        "audio_config_digest": audio_config_digest,
        "segment_count": segment_count,
        "total_speech_sec": total_speech_sec,
        "total_with_gaps_sec": total_with_gaps_sec,
    }
    index["revisions"] = list(index.get("revisions") or []) + [entry]
    evicted = _evict_revisions(store, index)
    save_revisions_index(store, index)
    return evicted


def _clear_active_dir(dir_path: Path) -> None:
    if not dir_path.is_dir():
        dir_path.mkdir(parents=True, exist_ok=True)
        return
    for p in dir_path.iterdir():
        if p.is_file():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


def _copy_revision_to_active(store: WorkspaceStore, revision_id: str) -> None:
    src_audio = revision_audio_dir(store, revision_id)
    src_sub = revision_subtitles_dir(store, revision_id)
    dst_audio = store.audio_active_dir
    dst_sub = store.subtitles_active_dir
    dst_audio.mkdir(parents=True, exist_ok=True)
    dst_sub.mkdir(parents=True, exist_ok=True)
    _clear_active_dir(dst_audio)
    _clear_active_dir(dst_sub)
    for wav in sorted(src_audio.glob("*.wav")):
        shutil.copy2(wav, dst_audio / wav.name)
    work = src_audio / "_work"
    if work.is_dir():
        shutil.rmtree(dst_audio / "_work", ignore_errors=True)
    for sub in sorted(src_sub.iterdir()):
        if sub.is_file():
            shutil.copy2(sub, dst_sub / sub.name)


def resolve_audio_config_digest(cfg: dict[str, Any] | None) -> str:
    data = cfg or {}
    return str(
        data.get("audio_config_digest")
        or data.get("audio_confirmed_digest")
        or data.get("audio_preview_digest")
        or ""
    )


def _write_active_audio_config(
    store: WorkspaceStore,
    revision_id: str,
    *,
    confirmed: bool,
) -> dict[str, Any]:
    meta = store.read_json(revision_meta_path(store, revision_id)) or {}
    digest = meta.get("audio_config_digest") or ""
    cfg: dict[str, Any] = {
        "revision_id": revision_id,
        "label": meta.get("label") or "",
        "preview": True,
        "confirmed": confirmed,
        "audio_config_digest": digest,
        "audio_preview_digest": digest,
        "audio_confirmed_digest": digest if confirmed else None,
        "segments": meta.get("segments") or [],
        "gaps_sec": meta.get("gaps_sec") or [],
        "speed": meta.get("speed") or 1.0,
        "texts": meta.get("texts") or [],
    }
    store.write_json(store.audio_config_path, cfg)
    return cfg


def promote_revision_to_active(
    store: WorkspaceStore,
    revision_id: str,
    *,
    confirmed: bool,
    invalidate_media: bool = True,
) -> dict[str, Any]:
    get_revision_entry(store, revision_id)
    _copy_revision_to_active(store, revision_id)
    cfg = _write_active_audio_config(store, revision_id, confirmed=confirmed)

    index = load_revisions_index(store)
    prev_active = index.get("active_revision_id")
    for item in index.get("revisions") or []:
        rid = item.get("revision_id")
        if rid == revision_id:
            item["status"] = "active"
        elif rid == prev_active and rid != revision_id:
            item["status"] = "draft"
    index["active_revision_id"] = revision_id
    save_revisions_index(store, index)

    store.flags.audio_ready = True
    store.flags.audio_confirmed = confirmed
    store.audio_preview_digest = cfg.get("audio_preview_digest")
    store.audio_confirmed_digest = cfg.get("audio_confirmed_digest")
    if invalidate_media:
        store.flags.media_bound = False
        store.flags.visual_preview_ready = False
        store.flags.compose_ready = False
        if store.media_binding_path.is_file():
            store.media_binding_path.unlink()

    return {
        "revision_id": revision_id,
        "label": cfg.get("label") or "",
        "audio_confirmed": confirmed,
        "audio_config_digest": cfg.get("audio_confirmed_digest") or cfg.get("audio_preview_digest"),
        "audio_preview_digest": cfg.get("audio_preview_digest"),
    }


def resolve_revision_id(
    store: WorkspaceStore,
    *,
    revision_id: str | None = None,
    audio_config_digest: str | None = None,
) -> str:
    if revision_id:
        get_revision_entry(store, revision_id)
        if audio_config_digest:
            entry = get_revision_entry(store, revision_id)
            if entry.get("audio_config_digest") != audio_config_digest:
                raise conflict(40901, "audio_config_digest mismatch")
        return revision_id
    if audio_config_digest:
        entry = find_revision_by_digest(store, audio_config_digest)
        if not entry:
            raise not_found("revision not found for digest")
        return str(entry["revision_id"])
    raise bad_request(40001, "revision_id or audio_config_digest is required")


def load_revision_audio_config(store: WorkspaceStore, revision_id: str) -> dict[str, Any]:
    meta = store.read_json(revision_meta_path(store, revision_id))
    if not meta:
        raise not_found("revision not found", revision_id=revision_id)
    return meta


def clear_all_audio_revisions(store: WorkspaceStore) -> None:
    index = load_revisions_index(store)
    for item in index.get("revisions") or []:
        _delete_revision_tree(store, str(item.get("revision_id") or ""))
    if revisions_index_path(store).is_file():
        revisions_index_path(store).unlink()
    for path in (store.audio_dir / "active", store.subtitles_dir / "active"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    rev_root_a = store.audio_dir / "revisions"
    if rev_root_a.is_dir():
        shutil.rmtree(rev_root_a, ignore_errors=True)
    rev_root_s = store.subtitles_dir / "revisions"
    if rev_root_s.is_dir():
        shutil.rmtree(rev_root_s, ignore_errors=True)
    for p in store.audio_dir.glob("*.wav"):
        p.unlink(missing_ok=True)
    for p in store.subtitles_dir.glob("subtitle.*"):
        p.unlink(missing_ok=True)
    if store.audio_config_path.is_file():
        store.audio_config_path.unlink()
