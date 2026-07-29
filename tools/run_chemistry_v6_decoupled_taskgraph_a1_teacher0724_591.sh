#!/usr/bin/env bash
set -uo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/fuxinzhou/prompt_test_chemistry}"
TAG="${TAG:-0729_v6_decoupled_taskgraph_a1_teacher0724_591_run1}"

cd "$PROJECT_ROOT" || exit 1

INPUT="${INPUT:-/data/fuxinzhou/chemistry/prepared/chemistry_teacher0724_591.jsonl}"
LABELS="${LABELS:-/data/fuxinzhou/chemistry/prepared/chemistry_labels_teacher_0724_591.csv}"
PROMPT="$PROJECT_ROOT/prompts/0730初中化学难度打标提示词_v6_decoupled_taskgraph_a1.txt"
RUNNER="$PROJECT_ROOT/src/chemistry_difficulty_rating_0730_v6_decoupled_taskgraph_a1_with_cache.py"
EVALUATOR="$PROJECT_ROOT/tools/evaluate_chemistry_difficulty.py"

RESULT="/data/fuxinzhou/chemistry/model_runs/${TAG}.jsonl"
ERRORS="/data/fuxinzhou/chemistry/model_runs/${TAG}_errors.jsonl"
LOG="/data/fuxinzhou/chemistry/logs/${TAG}.log"
REPORT_PREFIX="/data/fuxinzhou/chemistry/reports/${TAG}"
CACHE="/data/fuxinzhou/chemistry/cache/${TAG}_prompt_cache.json"

for required in "$INPUT" "$LABELS" "$PROMPT" "$RUNNER" "$EVALUATOR"; do
  if [[ ! -f "$required" ]]; then
    printf '缺失必要文件: %s\n' "$required" >&2
    exit 1
  fi
done

mkdir -p \
  /data/fuxinzhou/chemistry/model_runs \
  /data/fuxinzhou/chemistry/reports \
  /data/fuxinzhou/chemistry/logs \
  /data/fuxinzhou/chemistry/cache

export TEMPERATURE="${TEMPERATURE:-0}"
export CHEMISTRY_ENABLE_IMAGE_INPUT=1
export CHEMISTRY_DECOUPLED_POSTPROCESS_PROFILE="${CHEMISTRY_DECOUPLED_POSTPROCESS_PROFILE:-decoupled_schema_only}"
export CHEMISTRY_DECOUPLED_SCHEMA_RETRIES="${CHEMISTRY_DECOUPLED_SCHEMA_RETRIES:-2}"
export CHEMISTRY_DECOUPLED_JSON_RETRIES="${CHEMISTRY_DECOUPLED_JSON_RETRIES:-2}"
export CHEMISTRY_0724_V5_2_IMAGE_PRIMARY_CACHE_FILE="$CACHE"
export PYTHONUNBUFFERED=1

UV_PYTHON=(
  uv run
  --with aiofiles
  --with aiohttp
  --with tqdm
  --with python-dotenv
  --with json-repair
  python
)

"${UV_PYTHON[@]}" -m py_compile \
  "$RUNNER" \
  "$PROJECT_ROOT/src/chemistry_decoupled_taskgraph_schema.py" \
  "$EVALUATOR" || exit 1

set +e
"${UV_PYTHON[@]}" "$RUNNER" \
  -i "$INPUT" \
  -o "$RESULT" \
  -e "$ERRORS" \
  -p "$PROMPT" \
  2>&1 | tee "$LOG"
run_status=${PIPESTATUS[0]}
set -e

printf '程序退出状态: %s\n' "$run_status" | tee -a "$LOG"
if [[ "$run_status" -ne 0 ]]; then
  exit "$run_status"
fi

success_count=0
error_count=0
[[ -f "$RESULT" ]] && success_count=$(wc -l < "$RESULT")
[[ -f "$ERRORS" ]] && error_count=$(wc -l < "$ERRORS")
printf '成功题数: %s\n失败题数: %s\n总题数: %s\n' \
  "$success_count" "$error_count" "$((success_count + error_count))" | tee -a "$LOG"

"${UV_PYTHON[@]}" "$EVALUATOR" \
  --labels "$LABELS" \
  --predictions "$RESULT" \
  --errors "$ERRORS" \
  --level-source pre-postprocess \
  --report "${REPORT_PREFIX}_raw_evaluation.json" \
  --mismatches "${REPORT_PREFIX}_raw_mismatches.csv" | tee -a "$LOG"

"${UV_PYTHON[@]}" "$EVALUATOR" \
  --labels "$LABELS" \
  --predictions "$RESULT" \
  --errors "$ERRORS" \
  --report "${REPORT_PREFIX}_evaluation.json" \
  --mismatches "${REPORT_PREFIX}_mismatches.csv" | tee -a "$LOG"

ARCHIVE="$PROJECT_ROOT/${TAG}_results.tar.gz"
tar -czf "$ARCHIVE" \
  -C /data/fuxinzhou/chemistry/model_runs "${TAG}.jsonl" "${TAG}_errors.jsonl" \
  -C /data/fuxinzhou/chemistry/reports \
    "${TAG}_raw_evaluation.json" "${TAG}_raw_mismatches.csv" \
    "${TAG}_evaluation.json" "${TAG}_mismatches.csv" \
  -C /data/fuxinzhou/chemistry/logs "${TAG}.log"

printf '结果下载包: %s\n' "$ARCHIVE" | tee -a "$LOG"
