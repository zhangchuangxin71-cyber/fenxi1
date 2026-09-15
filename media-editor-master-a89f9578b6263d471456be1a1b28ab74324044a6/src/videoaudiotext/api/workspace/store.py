"""Workspace filesystem layout (Flow B v2.24)."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from videoaudiotext.config import PROJECT_ROOT

DEFAULT_WORKSPACES_ROOT = PROJECT_ROOT / "workspaces"

WORKSPACE_ID_MAX_LEN = 128
WORKSPACE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def validate_workspace_id(workspace_id: str) -> str:
    """Return a normalized workspace / task id or raise ValueError."""
    ws_id = workspace_id.strip()
    if not ws_id:
        raise ValueError("workspace_id must not be empty")
    if len(ws_id) > WORKSPACE_ID_MAX_LEN:
        raise ValueError(f"workspace_id must be at most {WORKSPACE_ID_MAX_LEN} characters")
    if ws_id.startswith("_"):
        raise ValueError("workspace_id must not start with '_'")
    if ".." in ws_id or "/" in ws_id or "\\" in ws_id:
        raise ValueError("workspace_id must not contain path separators")
    if not WORKSPACE_ID_RE.match(ws_id):
        raise ValueError(
            "workspace_id must start with a letter or digit and contain only "
            "letters, digits, '.', '_', or '-'"
        )
    return ws_id


def is_managed_workspace_directory(name: str) -> bool:
    """True if a directory name under WORKSPACES_ROOT is a user workspace."""
    if not name or name.startswith("_"):
        return False
    try:
        validate_workspace_id(name)
    except ValueError:
        return False
    return True


def workspaces_root() -> Path:
    raw = os.environ.get("WORKSPACES_ROOT", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return DEFAULT_WORKSPACES_ROOT.resolve()


@dataclass
class WorkspaceFlags:
    split_ready: bool = False
    audio_ready: bool = False
    audio_confirmed: bool = False
    media_bound: bool = False
    visual_preview_ready: bool = False
    compose_ready: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "split_ready": self.split_ready,
            "audio_ready": self.audio_ready,
            "audio_confirmed": self.audio_confirmed,
            "media_bound": self.media_bound,
            "visual_preview_ready": self.visual_preview_ready,
            "compose_ready": self.compose_ready,
        }


@dataclass
class WorkspaceStore:
    workspace_id: str
    root: Path
    label: str | None = None
    created_at: str = ""
    flags: WorkspaceFlags = field(default_factory=WorkspaceFlags)
    audio_preview_digest: str | None = None
    audio_confirmed_digest: str | None = None
    active_revision_id: str | None = None
    active_revision_label: str | None = None
    active_compose_job_id: str | None = None

    @classmethod
    def create(
        cls,
        label: str | None = None,
        *,
        workspace_id: str | None = None,
    ) -> WorkspaceStore:
        if workspace_id is not None:
            ws_id = validate_workspace_id(workspace_id)
        else:
            ws_id = f"ws_{uuid.uuid4().hex[:12]}"
        root = workspaces_root() / ws_id
        if root.exists():
            raise FileExistsError(ws_id)
        store = cls(
            workspace_id=ws_id,
            root=root,
            label=label,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        store.ensure_tree()
        store.save_meta()
        return store

    @classmethod
    def get_or_create(
        cls,
        workspace_id: str,
        *,
        label: str | None = None,
    ) -> tuple[WorkspaceStore, bool]:
        ws_id = validate_workspace_id(workspace_id)
        existing = cls.load(ws_id)
        if existing:
            return existing, False

        root = workspaces_root() / ws_id
        try:
            root.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            raced = cls.load(ws_id)
            if raced:
                return raced, False
            raise
        store = cls(
            workspace_id=ws_id,
            root=root,
            label=label,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        store.ensure_tree()
        store.save_meta()
        return store, True

    @classmethod
    def load(cls, workspace_id: str) -> WorkspaceStore | None:
        root = workspaces_root() / workspace_id
        if not root.is_dir():
            return None
        meta_path = root / "workspace_meta.json"
        label = None
        created_at = ""
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                label = meta.get("label")
                created_at = meta.get("created_at") or ""
            except (json.JSONDecodeError, OSError):
                pass
        store = cls(workspace_id=workspace_id, root=root, label=label, created_at=created_at)
        store.refresh_flags()
        return store

    def ensure_tree(self) -> None:
        for rel in (
            "config",
            "uploads",
            "source_media",
            "audio",
            "subtitles",
            "previews",
            "deliverables",
            "jobs",
            "intermediate",
        ):
            (self.root / rel).mkdir(parents=True, exist_ok=True)

    @property
    def meta_path(self) -> Path:
        return self.root / "workspace_meta.json"

    def read_meta(self) -> dict[str, Any]:
        data = self.read_json(self.meta_path)
        return data if data else {}

    def save_meta(self) -> None:
        self.ensure_tree()
        existing = self.read_meta()
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "workspace_id": self.workspace_id,
            "label": self.label,
            "created_at": self.created_at or existing.get("created_at") or now,
            "last_activity_at": existing.get("last_activity_at") or now,
            "compose_completed_at": existing.get("compose_completed_at"),
            "pinned": bool(existing.get("pinned", False)),
        }
        (self.meta_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def touch_activity(self) -> None:
        """Update last_activity_at in workspace_meta.json."""
        self.ensure_tree()
        meta = self.read_meta()
        if not meta.get("created_at") and self.created_at:
            meta["created_at"] = self.created_at
        if not meta.get("workspace_id"):
            meta["workspace_id"] = self.workspace_id
        if self.label is not None:
            meta["label"] = self.label
        meta["last_activity_at"] = datetime.now(timezone.utc).isoformat()
        self.meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def mark_compose_completed(self) -> None:
        """Record compose completion timestamp in workspace meta."""
        self.ensure_tree()
        meta = self.read_meta()
        now = datetime.now(timezone.utc).isoformat()
        meta.setdefault("workspace_id", self.workspace_id)
        meta["compose_completed_at"] = now
        meta["last_activity_at"] = now
        self.meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def is_pinned(self) -> bool:
        if self.read_meta().get("pinned"):
            return True
        raw = os.environ.get("WORKSPACE_PIN", "").strip()
        if not raw:
            return False
        pinned_ids = {x.strip() for x in raw.replace(",", " ").split() if x.strip()}
        return self.workspace_id in pinned_ids

    def _should_touch_activity(self, path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(self.root.resolve())
        except ValueError:
            return False
        if rel.as_posix() == "workspace_meta.json":
            return False
        if rel.parts and rel.parts[0] == "intermediate":
            return False
        return True

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def split_plan_path(self) -> Path:
        return self.config_dir / "split_plan.json"

    @property
    def audio_config_path(self) -> Path:
        return self.config_dir / "audio_config.json"

    @property
    def media_binding_path(self) -> Path:
        return self.config_dir / "media_binding.json"

    @property
    def render_style_path(self) -> Path:
        return self.config_dir / "render_style_config.json"

    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    @property
    def audio_active_dir(self) -> Path:
        active = self.audio_dir / "active"
        if (active / "master.wav").is_file():
            return active
        if (self.audio_dir / "master.wav").is_file():
            return self.audio_dir
        return active

    @property
    def subtitles_active_dir(self) -> Path:
        active = self.subtitles_dir / "active"
        if active.is_dir() and any(active.glob("*")):
            return active
        if self.subtitles_dir.is_dir() and any(self.subtitles_dir.glob("subtitle.*")):
            return self.subtitles_dir
        return active

    @property
    def subtitles_dir(self) -> Path:
        return self.root / "subtitles"

    @property
    def source_media_dir(self) -> Path:
        return self.root / "source_media"

    @property
    def previews_dir(self) -> Path:
        return self.root / "previews"

    @property
    def deliverables_dir(self) -> Path:
        return self.root / "deliverables"

    @property
    def output_mp4(self) -> Path:
        return self.deliverables_dir / "output.mp4"

    def read_json(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def write_json(self, path: Path, data: dict[str, Any]) -> None:
        from videoaudiotext.concurrency.coordination import write_json_atomic

        write_json_atomic(path, data)
        if self._should_touch_activity(path):
            self.touch_activity()

    @staticmethod
    def read_json_static(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def write_json_static(path: Path, data: dict[str, Any]) -> None:
        from videoaudiotext.concurrency.coordination import write_json_atomic

        write_json_atomic(path, data)

    def refresh_flags(self) -> WorkspaceFlags:
        split = self.read_json(self.split_plan_path)
        audio_cfg = self.read_json(self.audio_config_path)
        media = self.read_json(self.media_binding_path)
        render = self.read_json(self.render_style_path)
        master_ok = (self.audio_active_dir / "master.wav").is_file()
        flags = WorkspaceFlags(
            split_ready=bool(split),
            audio_ready=bool(audio_cfg and audio_cfg.get("preview") and master_ok),
            audio_confirmed=bool(
                audio_cfg and audio_cfg.get("confirmed") and master_ok
            ),
            media_bound=bool(media and media.get("bound")),
            visual_preview_ready=bool(render),
            compose_ready=self.output_mp4.is_file(),
        )
        self.flags = flags
        if audio_cfg:
            self.audio_preview_digest = audio_cfg.get("audio_preview_digest")
            self.audio_confirmed_digest = audio_cfg.get("audio_confirmed_digest")
            self.active_revision_id = audio_cfg.get("revision_id")
            self.active_revision_label = audio_cfg.get("label")
        rev_index = self.read_json(self.config_dir / "audio_revisions.json")
        if rev_index and not self.active_revision_id:
            self.active_revision_id = rev_index.get("active_revision_id")
        job_meta = self._active_job_meta()
        self.active_compose_job_id = job_meta.get("job_id") if job_meta else None
        return flags

    def _active_job_meta(self) -> dict[str, Any] | None:
        jobs_dir = self.root / "jobs"
        if not jobs_dir.is_dir():
            return None
        for p in sorted(
            jobs_dir.glob("compose_*.json"),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        ):
            data = self.read_json(p)
            if not data:
                continue
            if data.get("status") in ("queued", "running"):
                return data
        return None

    def delete_tree(self) -> None:
        import shutil

        if self.root.is_dir():
            shutil.rmtree(self.root)
