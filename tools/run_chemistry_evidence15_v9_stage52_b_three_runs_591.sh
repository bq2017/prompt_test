#!/usr/bin/env bash
set -euo pipefail

# Full formal experiment: generate three frozen Stage5 Compact runs, then apply
# the independent B auditor and deterministic T mapping to each run.
ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "${ROOT}" || exit 1

INPUT="${1:-${INPUT:-/data/fuxinzhou/chemistry/prepared/chemistry_teacher0724_591.jsonl}}"
LABELS="${2:-${LABELS:-/data/fuxinzhou/chemistry/prepared/chemistry_labels_teacher_0724_591.csv}}"
STAMP="${3:-$(date +%Y%m%d_%H%M%S)}"
STAGE5_OUTPUT_ROOT="${STAGE5_OUTPUT_ROOT:-/data/fuxinzhou/chemistry}"
STAGE52_OUTPUT_ROOT="${STAGE52_OUTPUT_ROOT:-/data/fuxinzhou/chemistry/stage52_b_${STAMP}}"

declare -a STAGE5_RESULTS=()

for run_number in 1 2 3; do
  tag="evidence15_v9_stage5_compact_591_stage52_run${run_number}_${STAMP}"
  OUTPUT_ROOT="${STAGE5_OUTPUT_ROOT}" \
    "${ROOT}/tools/run_chemistry_evidence15_v9_stage5_compact_teacher0724_591.sh" \
      "${INPUT}" "${LABELS}" "${tag}"

  stage5_result="${STAGE5_OUTPUT_ROOT}/model_runs/${tag}/${tag}.jsonl"
  if [[ ! -s "${stage5_result}" ]]; then
    echo "错误: Stage5 run${run_number}结果不存在或为空: ${stage5_result}" >&2
    exit 1
  fi
  STAGE5_RESULTS+=("${stage5_result}")

  OUTPUT_ROOT="${STAGE52_OUTPUT_ROOT}" \
    "${ROOT}/tools/run_chemistry_evidence15_v9_stage52_b_audit_591.sh" \
      "${stage5_result}" "${LABELS}" "run${run_number}" "${INPUT}"
done

uv run python "${ROOT}/tools/analyze_stage52_b_repeats.py" \
  --labels "${LABELS}" \
  --run run1 "${STAGE5_RESULTS[0]}" "${STAGE52_OUTPUT_ROOT}/run1/stage52_t.jsonl" "${STAGE52_OUTPUT_ROOT}/run1/run_manifest.json" \
  --run run2 "${STAGE5_RESULTS[1]}" "${STAGE52_OUTPUT_ROOT}/run2/stage52_t.jsonl" "${STAGE52_OUTPUT_ROOT}/run2/run_manifest.json" \
  --run run3 "${STAGE5_RESULTS[2]}" "${STAGE52_OUTPUT_ROOT}/run3/stage52_t.jsonl" "${STAGE52_OUTPUT_ROOT}/run3/run_manifest.json" \
  --output-json "${STAGE52_OUTPUT_ROOT}/repeated_summary.json" \
  --output-csv "${STAGE52_OUTPUT_ROOT}/repeated_question_level.csv"

echo "Stage5.2-B正式三次实验完成: ${STAGE52_OUTPUT_ROOT}"
