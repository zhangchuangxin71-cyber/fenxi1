#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-mineru-api-cpu}"
API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:8000}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-5}"
STATS_INTERVAL_SECONDS="${STATS_INTERVAL_SECONDS:-2}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MINERU_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HOST_OUTPUT_ROOT="${HOST_OUTPUT_ROOT:-${MINERU_ROOT}/output_cpu}"
HOST_BENCH_ROOT="${HOST_BENCH_ROOT:-${REPO_ROOT}/benchmarks/mineru_pipeline}"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <pdf-path-under-repo>" >&2
  exit 2
fi

PDF_REAL="$(realpath "$1")"
if [[ ! -f "${PDF_REAL}" ]]; then
  echo "PDF not found: ${PDF_REAL}" >&2
  exit 2
fi

case "${PDF_REAL}" in
  "${REPO_ROOT}"/*)
    PDF_REL="${PDF_REAL#${REPO_ROOT}/}"
    ;;
  *)
    echo "PDF must be under repo root mounted at /workspace: ${REPO_ROOT}" >&2
    exit 2
    ;;
esac

CONTAINER_PDF="/workspace/${PDF_REL}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"
BENCH_DIR="${HOST_BENCH_ROOT}/${RUN_ID}"
mkdir -p "${BENCH_DIR}"

STATS_FILE="${BENCH_DIR}/docker_stats.jsonl"
STATUS_LOG="${BENCH_DIR}/status.jsonl"
SUBMIT_FILE="${BENCH_DIR}/submit.json"
FINAL_STATUS_FILE="${BENCH_DIR}/final_status.json"
SUMMARY_FILE="${BENCH_DIR}/summary.json"

stats_pid=""
cleanup() {
  if [[ -n "${stats_pid}" ]]; then
    kill "${stats_pid}" 2>/dev/null || true
    wait "${stats_pid}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

sample_stats() {
  while true; do
    stats_json="$(docker stats "${CONTAINER_NAME}" --no-stream --format '{{json .}}' 2>/dev/null || true)"
    if [[ -n "${stats_json}" ]]; then
      printf '{"ts":"%s","stats":%s}\n' "$(date -Is)" "${stats_json}" >> "${STATS_FILE}"
    fi
    sleep "${STATS_INTERVAL_SECONDS}"
  done
}

sample_stats &
stats_pid="$!"

START_EPOCH="$(date +%s)"
START_ISO="$(date -Is)"

submit_response="$(
  docker exec "${CONTAINER_NAME}" curl -sS -X POST "${API_BASE_URL}/tasks" \
    -F "files=@${CONTAINER_PDF};type=application/pdf" \
    -F "lang_list=ch" \
    -F "backend=pipeline" \
    -F "parse_method=auto" \
    -F "formula_enable=true" \
    -F "table_enable=true" \
    -F "return_md=true" \
    -F "return_middle_json=true" \
    -F "return_model_output=true" \
    -F "return_content_list=true" \
    -F "return_images=false" \
    -F "response_format_zip=false"
)"
printf '%s\n' "${submit_response}" > "${SUBMIT_FILE}"

TASK_ID="$(python3 - "${SUBMIT_FILE}" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)
task_id = payload.get("task_id")
if not task_id:
    raise SystemExit(f"Missing task_id in submit response: {payload}")
print(task_id)
PY
)"

echo "task_id=${TASK_ID}"
echo "benchmark_dir=${BENCH_DIR}"

poll_count=0
while true; do
  poll_count=$((poll_count + 1))
  status_payload="$(docker exec "${CONTAINER_NAME}" curl -sS "${API_BASE_URL}/tasks/${TASK_ID}")"
  printf '{"ts":"%s","payload":%s}\n' "$(date -Is)" "${status_payload}" >> "${STATUS_LOG}"
  status="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("status", ""))' <<< "${status_payload}")"
  echo "poll=${poll_count} status=${status}"

  if [[ "${status}" == "completed" || "${status}" == "failed" ]]; then
    printf '%s\n' "${status_payload}" > "${FINAL_STATUS_FILE}"
    break
  fi
  sleep "${POLL_INTERVAL_SECONDS}"
done

END_EPOCH="$(date +%s)"
END_ISO="$(date -Is)"
WALL_SECONDS=$((END_EPOCH - START_EPOCH))

cleanup
trap - EXIT

python3 - "${STATS_FILE}" "${FINAL_STATUS_FILE}" "${SUMMARY_FILE}" "${HOST_OUTPUT_ROOT}" "${BENCH_DIR}" "${START_ISO}" "${END_ISO}" "${WALL_SECONDS}" <<'PY'
import json
import os
import shutil
import sys

stats_file, final_status_file, summary_file, host_output_root, bench_dir, start_iso, end_iso, wall_seconds = sys.argv[1:9]

def parse_percent(value):
    if value is None:
        return None
    text = str(value).strip().rstrip("%")
    try:
        return float(text)
    except ValueError:
        return None

def parse_size_to_mib(value):
    if not value:
        return None
    first = str(value).split("/", 1)[0].strip()
    units = {
        "B": 1 / (1024 * 1024),
        "KiB": 1 / 1024,
        "MiB": 1,
        "GiB": 1024,
        "TiB": 1024 * 1024,
        "kB": 1 / 1024,
        "MB": 1000 * 1000 / (1024 * 1024),
        "GB": 1000 * 1000 * 1000 / (1024 * 1024),
    }
    for unit, factor in sorted(units.items(), key=lambda item: len(item[0]), reverse=True):
        if first.endswith(unit):
            try:
                return float(first[: -len(unit)].strip()) * factor
            except ValueError:
                return None
    try:
        return float(first) / (1024 * 1024)
    except ValueError:
        return None

samples = []
if os.path.exists(stats_file):
    with open(stats_file, "r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                pass

cpu_values = []
mem_mib_values = []
mem_percent_values = []
pid_values = []
for sample in samples:
    stats = sample.get("stats") or {}
    cpu = parse_percent(stats.get("CPUPerc"))
    mem_mib = parse_size_to_mib(stats.get("MemUsage"))
    mem_percent = parse_percent(stats.get("MemPerc"))
    try:
        pids = int(stats.get("PIDs")) if stats.get("PIDs") is not None else None
    except ValueError:
        pids = None
    if cpu is not None:
        cpu_values.append(cpu)
    if mem_mib is not None:
        mem_mib_values.append(mem_mib)
    if mem_percent is not None:
        mem_percent_values.append(mem_percent)
    if pids is not None:
        pid_values.append(pids)

with open(final_status_file, "r", encoding="utf-8") as handle:
    final_status = json.load(handle)

task_id = final_status.get("task_id")
container_output_dir = final_status.get("output_dir")
if not container_output_dir and task_id:
    container_output_dir = f"/data/output/{task_id}"
host_output_dir = None
if isinstance(container_output_dir, str) and container_output_dir.startswith("/data/output"):
    suffix = container_output_dir.removeprefix("/data/output").lstrip("/")
    host_output_dir = os.path.join(host_output_root, suffix)

summary = {
    "task_id": task_id,
    "status": final_status.get("status"),
    "start_time": start_iso,
    "end_time": end_iso,
    "wall_seconds": int(wall_seconds),
    "container_output_dir": container_output_dir,
    "host_output_dir": host_output_dir,
    "benchmark_dir": bench_dir,
    "stats_samples": len(samples),
    "max_cpu_percent": max(cpu_values) if cpu_values else None,
    "avg_cpu_percent": sum(cpu_values) / len(cpu_values) if cpu_values else None,
    "max_mem_mib": max(mem_mib_values) if mem_mib_values else None,
    "avg_mem_mib": sum(mem_mib_values) / len(mem_mib_values) if mem_mib_values else None,
    "max_mem_percent": max(mem_percent_values) if mem_percent_values else None,
    "max_pids": max(pid_values) if pid_values else None,
    "error": final_status.get("error"),
}

archived_output_dirs = []
if os.environ.get("ARCHIVE_OUTPUT_ARTIFACTS", "true").lower() not in {"0", "false", "no"}:
    artifacts_root = os.path.join(bench_dir, "artifacts")
    if host_output_dir and os.path.isdir(host_output_dir):
        for current_root, dirnames, _filenames in os.walk(host_output_dir):
            if os.path.basename(current_root) != "auto":
                continue
            rel_parent = os.path.relpath(os.path.dirname(current_root), host_output_dir)
            target_dir = os.path.join(artifacts_root, rel_parent, "auto")
            shutil.copytree(current_root, target_dir, dirs_exist_ok=True)
            archived_output_dirs.append(target_dir)
            dirnames[:] = []

summary["archived_output_dirs"] = archived_output_dirs

with open(summary_file, "w", encoding="utf-8") as handle:
    json.dump(summary, handle, ensure_ascii=False, indent=2)

print(json.dumps(summary, ensure_ascii=False, indent=2))
PY
