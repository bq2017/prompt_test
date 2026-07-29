# Chemistry Evidence-15 V6 control baseline

This directory freezes the exact Evidence-15 V6 assets used by the reproducible
591-question control experiment.  It intentionally excludes the later V6.2
prompt/examples/schema experiment.

## Frozen experiment

- Control tag: `0731_v52_evidence15_v6_retest3_teacher0724_591`
- Dataset: teacher 2024-07-24, 591 questions
- Input mode: question and analysis images first, audited minimal text supplement
- Temperature: `0`
- Postprocess profile: `evidence15_boundary_rules_v6`
- Successful outputs: 589; failures: 2
- Raw exact: 367/591; raw strict accuracy: 62.0981%
- Final exact: 375/591; final strict accuracy: 63.4518%
- Postprocess net gain: 8 questions

Model serving can be nondeterministic even with temperature zero.  These numbers
identify the control run; a new run must report its own raw/final metrics.

## Production assets

- `prompts/0730初中化学难度打标提示词_v5_2_evidence15_v6.txt`
- `src/chemistry_difficulty_rating_0730_v5_2_evidence15_v6_with_cache.py`
- `src/chemistry_core12_schema.py`
- `tools/evaluate_chemistry_difficulty.py`

The input JSONL and teacher-label CSV are external evaluation data and are not
committed to Git.

## Static and contract checks

```bash
cd chemistry/evidence15_v6/tests
uv run --with aiofiles --with aiohttp --with tqdm --with python-dotenv --with json-repair \
  python -m unittest -v \
  test_prompt_core12_contract.py \
  test_core12_schema_retry.py
```

## Full 591-question control run

From the repository root:

```bash
bash chemistry/evidence15_v6/run_teacher0724_591.sh
```

Override the default experiment tag when needed:

```bash
TAG=0731_v52_evidence15_v6_control_teacher0724_591_run2 \
  bash chemistry/evidence15_v6/run_teacher0724_591.sh
```

The script validates files, runs the model, evaluates raw and final predictions,
and creates `${TAG}_results.tar.gz` in the repository root for download.
