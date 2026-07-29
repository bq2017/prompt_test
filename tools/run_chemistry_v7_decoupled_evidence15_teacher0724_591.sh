#!/usr/bin/env bash
set -uo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/fuxinzhou/prompt_test_chemistry_repo}"
TAG="${TAG:-0729_v7_independent_main_e15_teacher0724_591_run1}"
cd "$PROJECT_ROOT" || exit 1

INPUT="${INPUT:-/data/fuxinzhou/chemistry/prepared/chemistry_teacher0724_591.jsonl}"
LABELS="${LABELS:-/data/fuxinzhou/chemistry/prepared/chemistry_labels_teacher_0724_591.csv}"
PROMPT="$PROJECT_ROOT/prompts/0730初中化学难度打标提示词_v7_decoupled_evidence15.txt"
RUNNER="$PROJECT_ROOT/src/chemistry_difficulty_rating_0730_v7_decoupled_evidence15_with_cache.py"
POSTPROCESSOR="$PROJECT_ROOT/src/chemistry_postprocess_v7_decoupled.py"
TRANSPORT="$PROJECT_ROOT/src/chemistry_difficulty_rating_0730_v6_decoupled_taskgraph_a2_with_cache.py"
EVALUATOR="$PROJECT_ROOT/tools/evaluate_chemistry_difficulty.py"
ANALYZER="$PROJECT_ROOT/tools/analyze_chemistry_v7_postprocess.py"
TEST_FILE="$PROJECT_ROOT/tests/test_chemistry_v7_decoupled_evidence15.py"

RESULT="/data/fuxinzhou/chemistry/model_runs/${TAG}.jsonl"
ERRORS="/data/fuxinzhou/chemistry/model_runs/${TAG}_errors.jsonl"
LOG="/data/fuxinzhou/chemistry/logs/${TAG}.log"
REPORT_PREFIX="/data/fuxinzhou/chemistry/reports/${TAG}"
CACHE="/data/fuxinzhou/chemistry/cache/${TAG}_prompt_cache.json"

for required in "$INPUT" "$LABELS" "$PROMPT" "$RUNNER" "$POSTPROCESSOR" "$TRANSPORT" "$EVALUATOR" "$ANALYZER" "$TEST_FILE"; do
  if [[ ! -f "$required" ]]; then
    printf '缺少必要文件: %s\n' "$required" >&2
    exit 1
  fi
done

mkdir -p \
  /data/fuxinzhou/chemistry/model_runs \
  /data/fuxinzhou/chemistry/reports \
  /data/fuxinzhou/chemistry/logs \
  /data/fuxinzhou/chemistry/cache

touch "$RESULT" "$ERRORS" "$LOG"

export TEMPERATURE="${TEMPERATURE:-0}"
export CHEMISTRY_ENABLE_IMAGE_INPUT=1
export CHEMISTRY_V7_SCHEMA_RETRIES="${CHEMISTRY_V7_SCHEMA_RETRIES:-2}"
export CHEMISTRY_V7_JSON_RETRIES="${CHEMISTRY_V7_JSON_RETRIES:-2}"
export CHEMISTRY_V7_CONCURRENCY="${CHEMISTRY_V7_CONCURRENCY:-30}"
export CHEMISTRY_V7_POSTPROCESS_PROFILE="${CHEMISTRY_V7_POSTPROCESS_PROFILE:-conservative_v1}"
export CHEMISTRY_V7_CACHE_FILE="$CACHE"
export PYTHONUNBUFFERED=1

UV_PYTHON=(
  uv run
  --with aiofiles
  --with aiohttp
  --with tqdm
  --with python-dotenv
  python
)

"${UV_PYTHON[@]}" -m py_compile "$RUNNER" "$POSTPROCESSOR" "$TRANSPORT" "$EVALUATOR" "$ANALYZER" || exit 1
"${UV_PYTHON[@]}" "$TEST_FILE" || exit 1
"${UV_PYTHON[@]}" "$POSTPROCESSOR" || exit 1

printf 'V7架构: 单次调用 + 独立主定档 + 冻结后填写Evidence-15\n'
printf '后处理: %s\n' "$CHEMISTRY_V7_POSTPROCESS_PROFILE"
printf '并发数: %s\n' "$CHEMISTRY_V7_CONCURRENCY"

RUN_ARGS=(
  -i "$INPUT"
  -o "$RESULT"
  -e "$ERRORS"
  -p "$PROMPT"
  -c "$CHEMISTRY_V7_CONCURRENCY"
)
if [[ -n "${NUM:-}" ]]; then
  RUN_ARGS+=(-n "$NUM")
  printf '抽样题数: %s\n' "$NUM"
fi

set +e
"${UV_PYTHON[@]}" "$RUNNER" "${RUN_ARGS[@]}" 2>&1 | tee "$LOG"
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
printf '成功题数: %s\n失败题数: %s\n记录总数: %s\n' \
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

"${UV_PYTHON[@]}" "$ANALYZER" \
  --input "$RESULT" \
  --report "${REPORT_PREFIX}_postprocess_analysis.json" \
  --changes "${REPORT_PREFIX}_postprocess_changes.csv" | tee -a "$LOG"

ARCHIVE="$PROJECT_ROOT/${TAG}_results.tar.gz"
tar -czf "$ARCHIVE" \
  -C /data/fuxinzhou/chemistry/model_runs "${TAG}.jsonl" "${TAG}_errors.jsonl" \
  -C /data/fuxinzhou/chemistry/reports \
    "${TAG}_raw_evaluation.json" "${TAG}_raw_mismatches.csv" \
    "${TAG}_evaluation.json" "${TAG}_mismatches.csv" \
    "${TAG}_postprocess_analysis.json" "${TAG}_postprocess_changes.csv" \
  -C /data/fuxinzhou/chemistry/logs "${TAG}.log"

printf '结果下载包: %s\n' "$ARCHIVE" | tee -a "$LOG"
