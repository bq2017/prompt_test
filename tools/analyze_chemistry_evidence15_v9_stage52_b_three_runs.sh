#!/usr/bin/env bash
set -euo pipefail

ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "${ROOT}" || exit 1

LABELS="${1:?用法: $0 LABELS STAGE5_RUN1 STAGE5_RUN2 STAGE5_RUN3}"
STAGE5_RUN1="${2:?缺少Stage5 run1结果}"
STAGE5_RUN2="${3:?缺少Stage5 run2结果}"
STAGE5_RUN3="${4:?缺少Stage5 run3结果}"
OUTPUT_ROOT="${OUTPUT_ROOT:-/data/fuxinzhou/chemistry/stage52_b}"

for run_number in 1 2 3; do
  stage5_var="STAGE5_RUN${run_number}"
  stage5_path="${!stage5_var}"
  "${ROOT}/tools/run_chemistry_evidence15_v9_stage52_b_audit_591.sh" \
    "${stage5_path}" "${LABELS}" "run${run_number}"
done

uv run python "${ROOT}/tools/analyze_stage52_b_repeats.py" \
  --labels "${LABELS}" \
  --run run1 "${STAGE5_RUN1}" "${OUTPUT_ROOT}/run1/stage52_t.jsonl" "${OUTPUT_ROOT}/run1/run_manifest.json" \
  --run run2 "${STAGE5_RUN2}" "${OUTPUT_ROOT}/run2/stage52_t.jsonl" "${OUTPUT_ROOT}/run2/run_manifest.json" \
  --run run3 "${STAGE5_RUN3}" "${OUTPUT_ROOT}/run3/stage52_t.jsonl" "${OUTPUT_ROOT}/run3/run_manifest.json" \
  --output-json "${OUTPUT_ROOT}/repeated_summary.json" \
  --output-csv "${OUTPUT_ROOT}/repeated_question_level.csv"

echo "Stage5.2-B三次汇总完成: ${OUTPUT_ROOT}/repeated_summary.json"
