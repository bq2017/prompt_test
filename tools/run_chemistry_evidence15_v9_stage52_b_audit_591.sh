#!/usr/bin/env bash
set -euo pipefail

ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "${ROOT}" || exit 1

STAGE5_RESULTS="${1:?用法: $0 STAGE5_RESULTS LABELS RUN_NAME [INPUT_DATA]}"
LABELS="${2:?缺少教师标签CSV}"
RUN_NAME="${3:?缺少run名称，例如run1}"
INPUT_DATA="${4:-${INPUT:-/data/fuxinzhou/chemistry/prepared/chemistry_teacher0724_591.jsonl}}"
OUTPUT_ROOT="${OUTPUT_ROOT:-/data/fuxinzhou/chemistry/stage52_b}"
RUN_DIR="${OUTPUT_ROOT}/${RUN_NAME}"
CONCURRENCY="${CONCURRENCY:-30}"
MODEL_NAME="${MODEL_NAME:-doubao-seed-2.0-lite}"
TEMPERATURE="${TEMPERATURE:-}"

STAGE5_PROMPT="${ROOT}/prompts/evidence15_v9_stage5_compact_prompt.txt"
B_PROMPT="${ROOT}/prompts/evidence15_v9_stage52_b_audit_prompt.txt"
CANDIDATES="${RUN_DIR}/b_candidates.jsonl"
CANDIDATE_MANIFEST="${RUN_DIR}/candidate_manifest.json"
B_AUDIT="${RUN_DIR}/b_audit.jsonl"
B_ERRORS="${RUN_DIR}/b_errors.jsonl"
T_RESULTS="${RUN_DIR}/stage52_t.jsonl"
MAPPING_SUMMARY="${RUN_DIR}/mapping_summary.json"
RUN_MANIFEST="${RUN_DIR}/run_manifest.json"
LOG="${RUN_DIR}/stage52_b.log"

mkdir -p "${RUN_DIR}"
touch "${B_AUDIT}" "${B_ERRORS}" "${LOG}"

PYTHON=(
  uv run
  --with aiofiles
  --with aiohttp
  --with tqdm
  --with python-dotenv
  python
)

{
  "${PYTHON[@]}" -m py_compile \
    "${ROOT}/src/chemistry_stage52_b_schema.py" \
    "${ROOT}/src/chemistry_stage52_b_audit_runner.py" \
    "${ROOT}/tools/extract_stage52_b_candidates.py" \
    "${ROOT}/tools/apply_stage52_b_mapping.py" \
    "${ROOT}/tools/analyze_stage52_b_repeats.py" \
    "${ROOT}/tools/build_stage52_run_manifest.py"

  PYTHONPATH="${ROOT}/src:${ROOT}/tools${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON[@]}" -m unittest -v tests.test_evidence15_v9_stage52_b_audit

  "${PYTHON[@]}" "${ROOT}/tools/extract_stage52_b_candidates.py" \
    --stage5-results "${STAGE5_RESULTS}" \
    --output "${CANDIDATES}" \
    --manifest "${CANDIDATE_MANIFEST}"

  "${PYTHON[@]}" "${ROOT}/src/chemistry_stage52_b_audit_runner.py" \
    --input "${CANDIDATES}" \
    --output "${B_AUDIT}" \
    --error "${B_ERRORS}" \
    --prompt "${B_PROMPT}" \
    --concurrency "${CONCURRENCY}" \
    --cache-file "${RUN_DIR}/b_prompt_cache.json"

  "${PYTHON[@]}" "${ROOT}/tools/apply_stage52_b_mapping.py" \
    --stage5-results "${STAGE5_RESULTS}" \
    --b-audits "${B_AUDIT}" \
    --output "${T_RESULTS}" \
    --summary "${MAPPING_SUMMARY}"

  "${PYTHON[@]}" "${ROOT}/tools/build_stage52_run_manifest.py" \
    --output "${RUN_MANIFEST}" \
    --run-name "${RUN_NAME}" \
    --stage5-results "${STAGE5_RESULTS}" \
    --input-data "${INPUT_DATA}" \
    --stage5-prompt "${STAGE5_PROMPT}" \
    --b-prompt "${B_PROMPT}" \
    --candidate-script "${ROOT}/tools/extract_stage52_b_candidates.py" \
    --mapping-script "${ROOT}/tools/apply_stage52_b_mapping.py" \
    --audit-runner "${ROOT}/src/chemistry_stage52_b_audit_runner.py" \
    --schema-file "${ROOT}/src/chemistry_stage52_b_schema.py" \
    --schema-version "stage52_b_audit_v1" \
    --model-name "${MODEL_NAME}" \
    --temperature "${TEMPERATURE}" \
    --concurrency "${CONCURRENCY}" \
    --stage5-postprocess-profile "evidence15_boundary_rules_v9_stage5_audit45"

  "${PYTHON[@]}" "${ROOT}/tools/evaluate_chemistry_difficulty.py" \
    --labels "${LABELS}" \
    --predictions "${STAGE5_RESULTS}" \
    --level-source pre-postprocess \
    --report "${RUN_DIR}/s_evaluation.json" \
    --mismatches "${RUN_DIR}/s_mismatches.csv"

  "${PYTHON[@]}" "${ROOT}/tools/evaluate_chemistry_difficulty.py" \
    --labels "${LABELS}" \
    --predictions "${T_RESULTS}" \
    --level-source final \
    --report "${RUN_DIR}/t_evaluation.json" \
    --mismatches "${RUN_DIR}/t_mismatches.csv"
} 2>&1 | tee -a "${LOG}"

echo "Stage5.2-B单次审计完成: ${RUN_DIR}"
