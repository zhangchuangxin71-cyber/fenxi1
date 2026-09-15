from __future__ import annotations

import hashlib
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from app.agent_engine.errors import AgentEngineError


@dataclass(frozen=True, slots=True)
class _AssetSnapshot:
    relative_path: str
    content: str
    content_bytes: int
    sha256: str


class SkillLoader:
    """Validated, immutable L0/L1/L2 snapshot of build-time Skill assets."""

    def __init__(self, root: Path, *, manifest_name: str = "integration.yaml") -> None:
        self.root = root.resolve()
        self._manifest: dict[str, dict[str, Any]] = {}
        self._assets: dict[str, MappingProxyType[str, _AssetSnapshot]] = {}
        self._load_manifest(manifest_name)
        self._snapshot_assets()

    def _load_manifest(self, manifest_name: str) -> None:
        path = self.root / manifest_name
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise AgentEngineError(
                "AGENT_SKILL_MANIFEST_INVALID", f"Cannot load Skill manifest: {path}"
            ) from exc
        skills = raw.get("skills") if isinstance(raw, dict) else None
        if not isinstance(skills, list):
            raise AgentEngineError(
                "AGENT_SKILL_MANIFEST_INVALID", "Skill manifest must contain a skills list."
            )
        for item in skills:
            if not isinstance(item, dict) or not item.get("id") or not item.get("path"):
                raise AgentEngineError("AGENT_SKILL_MANIFEST_INVALID", "Every Skill needs an id and path.")
            skill_id = str(item["id"])
            if skill_id in self._manifest:
                raise AgentEngineError("AGENT_SKILL_DUPLICATE", f"Duplicate Skill: {skill_id}")
            resolved = (self.root / str(item["path"])).resolve()
            self._assert_inside(resolved)
            self._manifest[skill_id] = {**item, "resolved_path": resolved}

    def _snapshot_assets(self) -> None:
        for skill_id, item in self._manifest.items():
            base = item["resolved_path"]
            entrypoint = str(item.get("entrypoint", "SKILL.md"))
            candidates = {entrypoint}
            patterns = [str(value) for value in item.get("reference_allowlist", [])]
            for path in base.rglob("*"):
                if not path.is_file():
                    continue
                resolved = path.resolve()
                self._assert_inside(resolved)
                self._assert_inside_skill(resolved, base)
                relative = str(resolved.relative_to(base))
                if any(fnmatch(relative, pattern) for pattern in patterns):
                    candidates.add(relative)

            max_file_bytes = int(item.get("max_file_bytes", 65_536))
            max_total_bytes = int(item.get("max_total_bytes", 262_144))
            total_bytes = 0
            assets: dict[str, _AssetSnapshot] = {}
            for relative in sorted(candidates):
                path = (base / relative).resolve()
                self._assert_inside(path)
                self._assert_inside_skill(path, base)
                try:
                    content = path.read_text(encoding="utf-8")
                except OSError as exc:
                    raise AgentEngineError(
                        "AGENT_SKILL_READ_FAILED", f"Cannot load Skill asset: {skill_id}/{relative}"
                    ) from exc
                content_bytes = len(content.encode())
                if content_bytes > max_file_bytes:
                    raise AgentEngineError(
                        "AGENT_SKILL_TOO_LARGE",
                        f"Skill asset exceeds {max_file_bytes} bytes: {skill_id}/{relative}",
                    )
                total_bytes += content_bytes
                if total_bytes > max_total_bytes:
                    raise AgentEngineError(
                        "AGENT_SKILL_TOTAL_TOO_LARGE",
                        f"Skill assets exceed {max_total_bytes} bytes: {skill_id}",
                    )
                assets[relative] = _AssetSnapshot(
                    relative_path=relative,
                    content=content,
                    content_bytes=content_bytes,
                    sha256=hashlib.sha256(content.encode()).hexdigest(),
                )
            self._assets[skill_id] = MappingProxyType(assets)

    def metadata(self, skill_id: str, *, node_name: str) -> dict[str, Any]:
        item = self._resolve(skill_id, node_name=node_name)
        source = item.get("source", {})
        assets = self._assets[skill_id]
        return {
            "id": skill_id,
            "version": item.get("version", "unknown"),
            "capabilities": item.get("capabilities", []),
            "source": source,
            "manifest_sha256": hashlib.sha256(
                yaml.safe_dump(
                    {key: value for key, value in item.items() if key != "resolved_path"},
                    allow_unicode=True,
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
            "asset_sha256": hashlib.sha256(
                "".join(f"{name}:{asset.sha256}\n" for name, asset in assets.items()).encode()
            ).hexdigest(),
            "summary": item.get("summary", ""),
            "reference_allowlist": item.get("reference_allowlist", []),
        }

    def read(
        self,
        skill_id: str,
        *,
        node_name: str,
        level: str = "L1",
        reference: str | None = None,
    ) -> dict[str, Any]:
        item = self._resolve(skill_id, node_name=node_name)
        if level == "L0":
            return self.metadata(skill_id, node_name=node_name)
        if level == "L1":
            relative = str(item.get("entrypoint", "SKILL.md"))
        elif level == "L2" and reference:
            relative = self._reference_relative(item, reference)
        else:
            raise AgentEngineError(
                "AGENT_SKILL_REFERENCE_INVALID", "L2 requires an allowlisted reference path."
            )
        try:
            asset = self._assets[skill_id][relative]
        except KeyError as exc:
            raise AgentEngineError(
                "AGENT_SKILL_READ_FAILED", f"Skill asset is missing from the startup snapshot: {relative}"
            ) from exc
        return {
            **self.metadata(skill_id, node_name=node_name),
            "level": level,
            "path": asset.relative_path,
            "sha256": asset.sha256,
            "content": asset.content,
            "content_bytes": asset.content_bytes,
            "max_total_bytes": int(item.get("max_total_bytes", 262_144)),
        }

    def _resolve(self, skill_id: str, *, node_name: str) -> dict[str, Any]:
        try:
            item = self._manifest[skill_id]
        except KeyError as exc:
            raise AgentEngineError("AGENT_SKILL_UNKNOWN", f"Unknown Skill: {skill_id}") from exc
        if node_name not in set(item.get("allowed_nodes", [])):
            raise AgentEngineError(
                "AGENT_SKILL_NODE_FORBIDDEN", f"Skill {skill_id} is not allowed for {node_name}."
            )
        return item

    def _reference_relative(self, item: dict[str, Any], reference: str) -> str:
        path = (item["resolved_path"] / reference).resolve()
        try:
            relative = str(path.relative_to(item["resolved_path"]))
        except ValueError as exc:
            raise AgentEngineError(
                "AGENT_SKILL_REFERENCE_FORBIDDEN", "Reference escapes the Skill root."
            ) from exc
        patterns = [str(value) for value in item.get("reference_allowlist", [])]
        if not any(fnmatch(relative, pattern) for pattern in patterns):
            raise AgentEngineError(
                "AGENT_SKILL_REFERENCE_FORBIDDEN", f"Reference is not allowlisted: {relative}"
            )
        return relative

    def _assert_inside(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise AgentEngineError(
                "AGENT_SKILL_PATH_FORBIDDEN", "Skill path escapes the registered root."
            ) from exc

    @staticmethod
    def _assert_inside_skill(path: Path, base: Path) -> None:
        try:
            path.relative_to(base)
        except ValueError as exc:
            raise AgentEngineError(
                "AGENT_SKILL_PATH_FORBIDDEN", "Skill entrypoint escapes its registered directory."
            ) from exc
