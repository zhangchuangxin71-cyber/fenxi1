"""Build a small production manifest from the local evaluation-document subset.

This is a temporary operator script.  It deliberately locks writes to the
approved production tenant and ingestion endpoint used for this smoke test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

APPROVED_USER_ID = "user-demo-001"
APPROVED_KB_ID = "kb-demo"
INGESTION_BASE_URL = "http://39.108.184.150:8100"
INGESTION_API_PREFIX = "/ingestion/v1"
APPROVED_OSS_KEY_PREFIX = "prod/doc-ingestion/minerU/新建文件夹"
DEFAULT_DOCUMENT_DIR = Path("/workspace/新建文件夹")
DEFAULT_SOURCE_MANIFEST_DIR = Path("/workspace/rag-offline-eval/rag-eval-dataset/manifests")
DEFAULT_SOURCE_ARCHIVE_DIR = Path(
    "/workspace/rag-offline-eval/rag-eval-dataset/orig_docs_dataset/eval_rag_dataset"
)
DEFAULT_OUTPUT_DIR = Path("/workspace/production-manifests")
SUPPORTED_TYPES = {".pdf", ".docx", ".txt", ".md", ".markdown", ".html", ".xlsx", ".pptx"}
TERMINAL_STATES = {"completed", "failed", "cancelled"}
KNOWN_PRODUCTION_DOC_IDS = {
    (
        "0001_txt_34ae17a6d2_txt_0092__全国血防宣传周_吸血虫_血吸虫_您要仔细分清楚.txt"
    ): "76022cd1-39e0-5f96-b6d8-657c79c21c0f",
    (
        "0002_md_953c6d7650_md_0149_中华人民共和国全国人民代表大会组织法.md"
    ): "6314c3fc-b8a8-5619-97a0-f89e2308ec63",
}

# These two files are in the supplied OSS subset but have no query row in the
# current manifest.  They are added only after ingestion succeeds.  The
# questions are intentionally concrete and answerable from the source files.
MANUAL_QUERIES = {
    "0036_xlsx_6345d5c7bb_病毒分类举例__所有人.xlsx": [
        "传染性法氏囊病病毒的英文缩写是什么，属于哪个病毒科？"
    ],
    "0aabe0e1b156f530ec138a18d46eafa0476eb736e08a000d0576dee387a5ef46.docx": [
        "武定县2020年地方政府债务限额是多少，其中一般债券和专项债券债务限额分别是多少？"
    ],
    "第11讲 要素市场2.ppt(1) (1).pptx": ["在完全竞争的要素市场中，厂商使用生产要素的最优原则是什么？"],
}


@dataclass(frozen=True)
class SourceMatch:
    local_name: str
    source_doc_id: str
    source_doc_name: str
    manifest_dir: Path


def validate_production_scope(user_id: str, kb_id: str, base_url: str) -> None:
    if user_id != APPROVED_USER_ID or kb_id != APPROVED_KB_ID:
        raise ValueError("refusing production write: only user-demo-001/kb-demo are approved")
    if base_url.rstrip("/") != INGESTION_BASE_URL:
        raise ValueError(f"refusing production write: unexpected ingestion URL {base_url!r}")


def load_oss_key_prefix(config_path: Path) -> str:
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read config: {config_path}") from exc
    raw = payload.get("oss_key_prefix") if isinstance(payload, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"oss_key_prefix is missing from {config_path}")
    # The checked-out temporary config has one accidental leading backtick.
    # Normalize punctuation only, then require the exact approved prefix.
    prefix = raw.strip().strip("`").rstrip("/")
    if prefix != APPROVED_OSS_KEY_PREFIX:
        raise ValueError(f"unexpected oss_key_prefix: {prefix!r}")
    return prefix


def discover_documents(document_dir: Path) -> tuple[list[Path], list[dict[str, str]]]:
    if not document_dir.is_dir():
        raise ValueError(f"document directory not found: {document_dir}")
    documents: list[Path] = []
    skipped: list[dict[str, str]] = []
    for path in sorted(document_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".ppt":
            skipped.append({"file_name": path.name, "reason": "unsupported file type: .ppt"})
        elif suffix in SUPPORTED_TYPES:
            documents.append(path)
        else:
            skipped.append({"file_name": path.name, "reason": f"unsupported file type: {suffix or '<none>'}"})
    return documents, skipped


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_doc_id(path: Path) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sha256:{sha256_file(path)}"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"manifest file not found: {path}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"manifest row is not an object at {path}:{line_number}")
            rows.append(row)
    return rows


def match_source_documents(
    documents: Iterable[Path], archive_dir: Path, manifest_dir: Path
) -> dict[str, SourceMatch]:
    readable_rows = _read_jsonl(manifest_dir / "query_labels_readable.jsonl")
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in readable_rows:
        name = str(row.get("doc_name") or "")
        if name and row.get("doc_id"):
            by_name.setdefault(name, []).append(row)
    archive_hashes: dict[str, list[tuple[str, str]]] = {}
    for source_name in by_name:
        source_path = archive_dir / source_name
        if source_path.is_file():
            archive_hashes.setdefault(sha256_file(source_path), []).append(
                (source_name, str(by_name[source_name][0]["doc_id"]))
            )
    matches: dict[str, SourceMatch] = {}
    for path in documents:
        candidates = archive_hashes.get(sha256_file(path), [])
        if len(candidates) > 1:
            raise ValueError(f"ambiguous content match for {path.name}: {candidates}")
        if candidates:
            source_name, source_doc_id = candidates[0]
            matches[path.name] = SourceMatch(path.name, source_doc_id, source_name, manifest_dir)
    return matches


def rewrite_labels(
    matches: dict[str, SourceMatch],
    production_doc_ids: dict[str, str],
    manifest_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if manifest_dir is None:
        manifest_dir = next(iter(matches.values())).manifest_dir if matches else DEFAULT_SOURCE_MANIFEST_DIR
    old_to_new = {
        match.source_doc_id: production_doc_ids[match.local_name]
        for match in matches.values()
        if match.local_name in production_doc_ids
    }
    machine_rows = _read_jsonl(manifest_dir / "query_labels.jsonl")
    readable_rows = _read_jsonl(manifest_dir / "query_labels_readable.jsonl")
    machine = [
        {**row, "doc_id": old_to_new[row["doc_id"]]}
        for row in machine_rows
        if row.get("doc_id") in old_to_new
    ]
    expanded = []
    local_by_old = {match.source_doc_id: match.local_name for match in matches.values()}
    for row in readable_rows:
        old_id = row.get("doc_id")
        if old_id not in old_to_new:
            continue
        expanded.append({**row, "doc_id": old_to_new[old_id], "doc_name": local_by_old[old_id]})
    return machine, expanded


def add_manual_queries(
    machine: list[dict[str, Any]], expanded: list[dict[str, Any]], production_doc_ids: dict[str, str]
) -> None:
    next_number = len(machine) + 1
    for file_name, queries in MANUAL_QUERIES.items():
        doc_id = production_doc_ids.get(file_name)
        if not doc_id:
            continue
        for query in queries:
            query_id = f"prod_manual_{next_number:04d}"
            machine.append(
                {
                    "doc_id": doc_id,
                    "node_id": "",
                    "query_id": query_id,
                    "query_text": query,
                    "start_index": 1,
                    "end_index": 1,
                }
            )
            expanded.append(
                {
                    "doc_id": doc_id,
                    "doc_name": file_name,
                    "node_id": "",
                    "query_id": query_id,
                    "query_text": query,
                    "start_index": 1,
                    "end_index": 1,
                    "context_source": "manual",
                }
            )
            next_number += 1


def _json_dump(path: Path, value: Any) -> None:
    _write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _write_text_atomic(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def load_results(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("documents"), dict):
        raise ValueError(f"invalid ingestion checkpoint: {path}")
    return payload["documents"]


def validate_delivery(
    results: dict[str, Any],
    machine: list[dict[str, Any]],
    expanded: list[dict[str, Any]],
) -> None:
    incomplete = sorted(name for name, item in results.items() if item.get("status") != "completed")
    if incomplete:
        raise ValueError(f"documents are not completed: {incomplete}")
    result_doc_ids = {str(item.get("doc_id") or "") for item in results.values()}
    if "" in result_doc_ids or len(result_doc_ids) != len(results):
        raise ValueError("completed documents must have distinct non-empty doc_ids")
    required = ("query_id", "query_text", "doc_id")
    if any(not all(row.get(key) for key in required) for row in machine):
        raise ValueError("machine manifest contains incomplete query labels")
    query_ids = [str(row["query_id"]) for row in machine]
    if len(query_ids) != len(set(query_ids)):
        raise ValueError("query_id values must be unique")
    label_doc_ids = {str(row["doc_id"]) for row in machine}
    if label_doc_ids != result_doc_ids:
        raise ValueError("query coverage must include every completed production document exactly by doc_id")
    core_fields = ("query_id", "query_text", "doc_id", "node_id", "start_index", "end_index")
    machine_core = {tuple(row.get(field) for field in core_fields) for row in machine}
    expanded_core = {tuple(row.get(field) for field in core_fields) for row in expanded}
    if len(machine) != len(expanded) or machine_core != expanded_core:
        raise ValueError("machine and readable manifests disagree")


class HttpResponseError(RuntimeError):
    def __init__(self, status_code: int, url: str, detail: str) -> None:
        super().__init__(f"HTTP {status_code} from {url}: {detail}")
        self.status_code = status_code


class IngestionClient:
    def __init__(self, base_url: str, user_id: str, kb_id: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.kb_id = kb_id
        self.timeout = timeout

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{INGESTION_API_PREFIX}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url, data=data, method=method, headers={"Content-Type": "application/json"} if data else {}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HttpResponseError(exc.code, url, detail) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"request failed for {url}: {exc.reason}") from exc
        if not isinstance(body, dict):
            raise RuntimeError(f"unexpected response from {url}: {body!r}")
        return body

    def submit(self, *, oss_key: str, file_name: str, file_type: str, idempotency_key: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/documents/ingest",
            {
                "user_id": self.user_id,
                "kb_id": self.kb_id,
                "oss_key": oss_key,
                "file_name": file_name,
                "file_type": file_type,
                "idempotency_key": idempotency_key,
            },
        )

    def status_by_doc(self, doc_id: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"user_id": self.user_id, "kb_id": self.kb_id})
        return self.request("GET", f"/documents/{urllib.parse.quote(doc_id, safe='')}/status?{query}")

    def status_by_task(self, task_id: str) -> dict[str, Any]:
        return self.request("GET", f"/tasks/{urllib.parse.quote(task_id, safe='')}")

    def find_status_by_doc(self, doc_id: str) -> dict[str, Any] | None:
        try:
            return self.status_by_doc(doc_id)
        except HttpResponseError as exc:
            if exc.status_code == 404:
                return None
            raise


def wait_for_completion(
    client: IngestionClient, task_id: str, poll_seconds: float, timeout_seconds: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        body = client.status_by_task(task_id)
        data = body.get("data") or {}
        status = str(data.get("status") or "").lower()
        if status in TERMINAL_STATES:
            if status != "completed":
                raise RuntimeError(
                    f"ingestion task {task_id} ended as {status}: {data.get('error_message')}; "
                    "inspect the production task before any explicit retry or cleanup"
                )
            return data
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for ingestion task {task_id}")
        time.sleep(poll_seconds)


def build_payloads(documents: list[Path], prefix: str, user_id: str, kb_id: str) -> list[dict[str, str]]:
    return [
        {
            "file_name": path.name,
            "file_type": path.suffix.lower().lstrip("."),
            "oss_key": f"{prefix}/{path.name}",
            "expected_doc_id": stable_doc_id(path),
            "user_id": user_id,
            "kb_id": kb_id,
        }
        for path in documents
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents-dir", type=Path, default=DEFAULT_DOCUMENT_DIR)
    parser.add_argument("--source-manifest-dir", type=Path, default=DEFAULT_SOURCE_MANIFEST_DIR)
    parser.add_argument("--source-archive-dir", type=Path, default=DEFAULT_SOURCE_ARCHIVE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.yaml"))
    parser.add_argument("--user-id", default=APPROVED_USER_ID)
    parser.add_argument("--kb-id", default=APPROVED_KB_ID)
    parser.add_argument("--ingestion-base-url", default=INGESTION_BASE_URL)
    parser.add_argument("--confirm-production-write", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    prefix = load_oss_key_prefix(args.config)
    validate_production_scope(args.user_id, args.kb_id, args.ingestion_base_url)
    if not args.dry_run and not args.confirm_production_write:
        raise ValueError("pass --confirm-production-write before writing production")
    documents, skipped = discover_documents(args.documents_dir)
    matches = match_source_documents(documents, args.source_archive_dir, args.source_manifest_dir)
    payloads = build_payloads(documents, prefix, args.user_id, args.kb_id)
    print(
        json.dumps(
            {"documents": payloads, "skipped": skipped, "matched_annotations": sorted(matches)},
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.dry_run:
        return 0

    client = IngestionClient(args.ingestion_base_url, args.user_id, args.kb_id)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result_path = args.output_dir / "ingestion_results.json"
    results = load_results(result_path)
    current_names = {payload["file_name"] for payload in payloads}
    results = {name: item for name, item in results.items() if name in current_names}
    production_doc_ids: dict[str, str] = {}
    for payload in payloads:
        name = payload["file_name"]
        expected = payload["expected_doc_id"]
        if name in KNOWN_PRODUCTION_DOC_IDS:
            if expected != KNOWN_PRODUCTION_DOC_IDS[name]:
                raise ValueError(f"known doc_id does not match content for {name}")
            status = client.status_by_doc(expected)
            status_data = status.get("data") or {}
            if status_data.get("status") != "completed":
                task_id = status_data.get("task_id")
                if not task_id:
                    raise RuntimeError(f"known document is incomplete and has no task: {name}")
                status_data = wait_for_completion(
                    client, str(task_id), args.poll_seconds, args.timeout_seconds
                )
            results[name] = {
                "doc_id": expected,
                "status": status_data.get("status"),
                "known": True,
                "oss_key": payload["oss_key"],
            }
            production_doc_ids[name] = expected
        elif name in results and results[name].get("doc_id"):
            checkpoint_doc_id = str(results[name]["doc_id"])
            if checkpoint_doc_id != expected:
                raise ValueError(f"checkpoint doc_id does not match content for {name}")
            status_body = client.find_status_by_doc(expected)
            if status_body is None:
                raise RuntimeError(f"checkpoint document is missing from production: {name}")
            status = status_body.get("data") or {}
            if status.get("status") != "completed":
                task_id = status.get("task_id")
                if not task_id:
                    raise RuntimeError(f"cannot resume incomplete document: {name}")
                status = wait_for_completion(client, str(task_id), args.poll_seconds, args.timeout_seconds)
            results[name]["status"] = status.get("status")
            production_doc_ids[name] = expected
        else:
            existing = client.find_status_by_doc(expected)
            if existing is not None:
                data = existing.get("data") or {}
                task_id = data.get("task_id")
                if data.get("status") == "completed":
                    status = data
                elif task_id:
                    status = wait_for_completion(
                        client, str(task_id), args.poll_seconds, args.timeout_seconds
                    )
                else:
                    raise RuntimeError(f"existing document has no resumable task: {name}")
                doc_id = expected
            else:
                key_digest = hashlib.sha256(payload["oss_key"].encode("utf-8")).hexdigest()[:24]
                response = client.submit(
                    oss_key=payload["oss_key"],
                    file_name=name,
                    file_type=payload["file_type"],
                    idempotency_key=f"production-manifest-{key_digest}",
                )
                data = response.get("data") or {}
                doc_id = str(data.get("doc_id") or expected)
                if doc_id != expected:
                    raise RuntimeError(f"production doc_id does not match local content for {name}: {doc_id}")
                task_id = data.get("task_id")
                if task_id:
                    status = wait_for_completion(
                        client, str(task_id), args.poll_seconds, args.timeout_seconds
                    )
                else:
                    status = client.status_by_doc(doc_id).get("data") or {}
                    if status.get("status") != "completed":
                        resumed_task_id = status.get("task_id")
                        if not resumed_task_id:
                            raise RuntimeError(
                                f"submit response for {name} has no completed status or task_id"
                            )
                        status = wait_for_completion(
                            client,
                            str(resumed_task_id),
                            args.poll_seconds,
                            args.timeout_seconds,
                        )
            results[name] = {
                "doc_id": doc_id,
                "task_id": task_id,
                "status": status.get("status"),
                "oss_key": payload["oss_key"],
                "known": False,
            }
            production_doc_ids[name] = doc_id
        _json_dump(
            result_path,
            {"user_id": args.user_id, "kb_id": args.kb_id, "documents": results, "skipped": skipped},
        )

    machine, expanded = rewrite_labels(matches, production_doc_ids, args.source_manifest_dir)
    unannotated = set(production_doc_ids) - set(matches)
    if unannotated != set(MANUAL_QUERIES):
        raise ValueError(f"manual query mapping does not match unannotated documents: {sorted(unannotated)}")
    add_manual_queries(machine, expanded, production_doc_ids)
    validate_delivery(results, machine, expanded)
    _json_dump(
        args.output_dir / "manifest_metadata.json",
        {
            "user_id": args.user_id,
            "kb_id": args.kb_id,
            "oss_key_prefix": prefix,
            "skipped": skipped,
            "unannotated_documents": sorted(unannotated),
            "source": str(args.source_manifest_dir),
        },
    )
    _write_text_atomic(
        args.output_dir / "query_labels.jsonl",
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in machine),
    )
    _write_text_atomic(
        args.output_dir / "query_labels_readable.jsonl",
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in expanded),
    )
    print(f"wrote {len(machine)} query labels to {args.output_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TimeoutError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
