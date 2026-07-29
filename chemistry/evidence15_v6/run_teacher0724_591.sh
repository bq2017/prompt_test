#!/usr/bin/env bash
set -uo pipefail

PACKAGE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$PACKAGE_DIR/../.." && pwd)

TAG=${TAG:-0731_v52_evidence15_v6_control_teacher0724_591_run1}
INPUT=${INPUT:-/data/fuxinzhou/chemistry/prepared/chemistry_teacher0724_591.jsonl}
LABELS=${LABELS:-/data/fuxinzhou/chemistry/prepared/chemistry_labels_teacher_0724_591.csv}
DATA_ROOT=${DATA_ROOT:-/data/fuxinzhou/chemistry}

PROMPT="$PACKAGE_DIR/prompts/0730初中化学难度打标提示词_v5_2_evidence15_v6.txt"
RUNNER="$PACKAGE_DIR/src/chemistry_difficulty_rating_0730_v5_2_evidence15_v6_with_cache.py"
SCHEMA="$PACKAGE_DIR/src/chemistry_core12_schema.py"
EVALUATOR="$PACKAGE_DIR/tools/evaluate_chemistry_difficulty.py"

RESULT="$DATA_ROOT/model_runs/${TAG}.jsonl"
ERRORS="$DATA_ROOT/model_runs/${TAG}_errors.jsonl"
LOG="$DATA_ROOT/logs/${TAG}.log"
REPORT_PREFIX="$DATA_ROOT/reports/${TAG}"
CACHE="$DATA_ROOT/cache/${TAG}_prompt_cache.json"
EXPORT_DIR="$DATA_ROOT/exports/${TAG}"
ARCHIVE="$PROJECT_ROOT/${TAG}_results.tar.gz"

for file in "$INPUT" "$LABELS" "$PROMPT" "$RUNNER" "$SCHEMA" "$EVALUATOR"; do
  if [ ! -f "$file" ]; then
    printf 'Missing required file: %s\n' "$file"
    exit 1
  fi
done

cd "$PROJECT_ROOT" || exit 1
mkdir -p \
  "$DATA_ROOT/model_runs" \
  "$DATA_ROOT/reports" \
  "$DATA_ROOT/logs" \
  "$DATA_ROOT/cache" \
  "$EXPORT_DIR"
touch "$ERRORS"

export TEMPERATURE=0
export CHEMISTRY_ENABLE_IMAGE_INPUT=1
export CHEMISTRY_CORE12_POSTPROCESS_PROFILE=evidence15_boundary_rules_v6
export CHEMISTRY_CORE12_SAFE_RULES_APPROVED=1
export CHEMISTRY_CORE12_ALLOW_LEGACY_SCHEMA=0
export CHEMISTRY_CORE12_SCHEMA_RETRIES=2
export CHEMISTRY_CORE12_JSON_RETRIES=2
export CHEMISTRY_0724_V5_2_IMAGE_PRIMARY_POSTPROCESS_PROFILE=prompt_only
export CHEMISTRY_0724_V5_2_IMAGE_PRIMARY_CACHE_FILE="$CACHE"
export PYTHONUNBUFFERED=1

UV_DEPS=(
  --with aiofiles
  --with aiohttp
  --with tqdm
  --with python-dotenv
  --with json-repair
)

uv run "${UV_DEPS[@]}" python -m py_compile "$RUNNER" "$SCHEMA"

set -o pipefail
uv run "${UV_DEPS[@]}" python "$RUNNER" \
  -p "$PROMPT" \
  -i "$INPUT" \
  -o "$RESULT" \
  -e "$ERRORS" \
  2>&1 | tee -a "$LOG"
run_status=${PIPESTATUS[0]}

printf 'Program exit status: %s\n' "$run_status" | tee -a "$LOG"
if [ "$run_status" -ne 0 ]; then
  exit "$run_status"
fi

success_count=0
error_count=0
[ -f "$RESULT" ] && success_count=$(wc -l < "$RESULT")
[ -f "$ERRORS" ] && error_count=$(wc -l < "$ERRORS")
printf 'Success: %s\nFailure: %s\nTotal: %s\n' \
  "$success_count" "$error_count" "$((success_count + error_count))" | tee -a "$LOG"

uv run "${UV_DEPS[@]}" python "$EVALUATOR" \
  --labels "$LABELS" \
  --predictions "$RESULT" \
  --errors "$ERRORS" \
  --level-source pre-postprocess \
  --report "${REPORT_PREFIX}_raw_evaluation.json" \
  --mismatches "${REPORT_PREFIX}_raw_mismatches.csv"

uv run "${UV_DEPS[@]}" python "$EVALUATOR" \
  --labels "$LABELS" \
  --predictions "$RESULT" \
  --errors "$ERRORS" \
  --report "${REPORT_PREFIX}_evaluation.json" \
  --mismatches "${REPORT_PREFIX}_mismatches.csv"

for file in \
  "$RESULT" \
  "$ERRORS" \
  "$LOG" \
  "${REPORT_PREFIX}_raw_evaluation.json" \
  "${REPORT_PREFIX}_raw_mismatches.csv" \
  "${REPORT_PREFIX}_evaluation.json" \
  "${REPORT_PREFIX}_mismatches.csv"; do
  [ -f "$file" ] && cp -f "$file" "$EXPORT_DIR/"
done

tar -czf "$ARCHIVE" -C "$EXPORT_DIR" .
printf 'Result archive: %s\n' "$ARCHIVE"
