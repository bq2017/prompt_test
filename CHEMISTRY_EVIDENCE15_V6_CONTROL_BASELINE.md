# Chemistry Evidence-15 V6 control baseline

This snapshot preserves the exact Evidence-15 V6 configuration used as the
control baseline before the next prompt redesign.

## Versioned files

- `prompts/0730初中化学难度打标提示词_v5_2_evidence15_v6.txt`
- `src/chemistry_difficulty_rating_0730_v5_2_evidence15_v6_with_cache.py`
- `src/chemistry_core12_schema.py`
- `tools/evaluate_chemistry_difficulty.py`

## Source snapshot SHA-256

- Prompt: `12689B324181F73D2F454FFFB7EEA425197787CA001D42E05D9CAC1B57381B5B`
- Runner: `CB7B90D9CDB0FFB8F00C3B3E447E903A234531CC8E0DBA82189958CC5320F70B`
- Schema/postprocessor: `1544D21948983AFBAC60C179592025B64DDE295DE0E31043D950993CF62DA9C8`
- Evaluator: `3BD0C4170FF7B4A101808D3D24AC54535BC0FADD9F52459523C626D4B2518EF3`

## Reproduced experiment identity

- Experiment tag: `0731_v52_evidence15_v6_retest3_teacher0724_591`
- Dataset: teacher 2026-07-24, 591 questions
- Input: image primary with audited minimal text supplement
- Raw strict accuracy: `367/591 = 62.0981%`
- Postprocessed strict accuracy: `375/591 = 63.4518%`
- Successful outputs: 589
- Failed outputs: 2

Do not modify these V6 files in place for the next experiment. Create a new
prompt and runner version so the baseline remains reproducible and diffable.
