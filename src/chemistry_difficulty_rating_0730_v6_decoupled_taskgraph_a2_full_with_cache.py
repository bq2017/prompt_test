"""Entry point for the composed A2-full chemistry experiment.

It reuses the frozen A2 two-pass transport and strict schema, supplies the
expanded prompt pair, and attaches audit-only physics-inspired structural
checks. No audit rule can change the model level.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import chemistry_difficulty_rating_0730_v6_decoupled_taskgraph_a2_with_cache as core
from chemistry_a2_full_audit import apply_a2_full_audit


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FULL_TASK_PROMPT = PROJECT_ROOT / "prompts" / "0730初中化学任务重建提示词_v6_decoupled_taskgraph_a2_full.txt"
FULL_RATING_PROMPT = PROJECT_ROOT / "prompts" / "0730初中化学难度打标提示词_v6_decoupled_taskgraph_a2_full.txt"


def load_composed_prompt(path: str, prefix_name: str, suffix_name: str) -> tuple[str, str]:
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"找不到Prompt文件: {source}")
    namespace: dict[str, Any] = {"__file__": str(source)}
    exec(source.read_text(encoding="utf-8"), namespace)
    prefix = str(namespace.get(prefix_name, "")).strip()
    suffix = str(namespace.get(suffix_name, "")).strip()
    if not prefix or not suffix:
        raise ValueError(f"Prompt缺少{prefix_name}或{suffix_name}: {source}")
    return prefix, suffix


_base_validate_rating = core.validate_rating


def validate_rating_with_full_audit(value: Any, reconstruction: Any) -> dict[str, Any]:
    rating = _base_validate_rating(value, reconstruction)
    return apply_a2_full_audit(rating, reconstruction)


core.DEFAULT_TASK_PROMPT = FULL_TASK_PROMPT
core.DEFAULT_RATING_PROMPT = FULL_RATING_PROMPT
core.load_prompt = load_composed_prompt
core.validate_rating = validate_rating_with_full_audit
if not os.getenv("CHEMISTRY_A2_CACHE_FILE", "").strip():
    core.CACHE_FILE_PATH = PROJECT_ROOT / "chemistry_v6_decoupled_taskgraph_a2_full_prompt_cache.json"


if __name__ == "__main__":
    print("A2-full: two independent calls + expanded chemistry calibration + audit-only structural flags")
    print("A2-full automatic level changes: disabled")
    started = time.time()
    asyncio.run(core.main_batch_run())
    print(f"总耗时: {(time.time() - started) / 60:.2f} 分钟")
