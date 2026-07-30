#!/usr/bin/env bash
set -euo pipefail

ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "${ROOT}" || exit 1
INPUT="${1:-${INPUT:-/data/fuxinzhou/chemistry/prepared/chemistry_teacher0724_591.jsonl}}"
LABELS="${2:-${LABELS:-/data/fuxinzhou/chemistry/prepared/chemistry_labels_teacher_0724_591.csv}}"
TAG="${3:-evidence15_v9_stage3_591_$(date +%Y%m%d_%H%M%S)}"
OUTPUT_ROOT="${OUTPUT_ROOT:-/data/fuxinzhou/chemistry}"
OUT_DIR="${OUTPUT_ROOT}/model_runs/${TAG}"
RESULT="${OUT_DIR}/${TAG}.jsonl"
ERRORS="${OUT_DIR}/${TAG}_errors.jsonl"
LOG="${OUTPUT_ROOT}/logs/${TAG}.log"
REPORT_DIR="${OUTPUT_ROOT}/reports/${TAG}"

mkdir -p "${OUT_DIR}" "$(dirname "${LOG}")" "${REPORT_DIR}"
touch "${RESULT}" "${ERRORS}" "${LOG}"

export CHEMISTRY_EVIDENCE15_V9_POSTPROCESS_PROFILE="evidence15_boundary_rules_v9_stage1"
export PYTHONUNBUFFERED=1

PYTHON=(
  uv run
  --with aiofiles
  --with aiohttp
  --with tqdm
  --with python-dotenv
  python
)

"${PYTHON[@]}" -m py_compile \
  "${ROOT}/src/chemistry_evidence15_v9_schema.py" \
  "${ROOT}/src/chemistry_difficulty_rating_evidence15_v9_with_cache.py" \
  "${ROOT}/tools/evaluate_chemistry_difficulty.py"

PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}" \
  "${PYTHON[@]}" -m unittest -v \
    tests.test_prompt_evidence15_v9_contract \
    tests.test_evidence15_v9_schema_retry \
    tests.test_evidence15_v9_stage2 \
    tests.test_evidence15_v9_stage21 \
    tests.test_evidence15_v9_stage22 \
    tests.test_evidence15_v9_stage23 \
    tests.test_evidence15_v9_stage3

{
  echo "标准标签来源: 教师清洗CSV ${LABELS} 的 standard_level"
  echo "重要: 结果JSONL顶层 difficulty 是旧输入错误标签，正式评测一律忽略"

  "${PYTHON[@]}" "${ROOT}/src/chemistry_difficulty_rating_evidence15_v9_with_cache.py" \
    --prompt "${ROOT}/prompts/evidence15_v9_prompt.txt" \
    --input "${INPUT}" \
    --output "${RESULT}" \
    --error "${ERRORS}" \
    --concurrency 30

  "${PYTHON[@]}" "${ROOT}/tools/evaluate_chemistry_difficulty.py" \
    --labels "${LABELS}" \
    --predictions "${RESULT}" \
    --errors "${ERRORS}" \
    --level-source pre-postprocess \
    --report "${REPORT_DIR}/${TAG}_raw_evaluation.json" \
    --mismatches "${REPORT_DIR}/${TAG}_raw_mismatches.csv"

  "${PYTHON[@]}" "${ROOT}/tools/evaluate_chemistry_difficulty.py" \
    --labels "${LABELS}" \
    --predictions "${RESULT}" \
    --errors "${ERRORS}" \
    --level-source final \
    --report "${REPORT_DIR}/${TAG}_evaluation.json" \
    --mismatches "${REPORT_DIR}/${TAG}_mismatches.csv"
} 2>&1 | tee "${LOG}"

ARCHIVE="${ROOT}/${TAG}_results.tar.gz"
tar -czf "${ARCHIVE}" \
  -C "${OUT_DIR}" "${TAG}.jsonl" "${TAG}_errors.jsonl" \
  -C "${REPORT_DIR}" \
    "${TAG}_raw_evaluation.json" "${TAG}_raw_mismatches.csv" \
    "${TAG}_evaluation.json" "${TAG}_mismatches.csv" \
  -C "$(dirname "${LOG}")" "${TAG}.log"

echo "V9阶段3打标完成：${OUT_DIR}"
echo "结果包：${ARCHIVE}"
