"""Extract only raw Stage5 Compact level-4 questions for independent B audit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, Mapping


ALLOWED_QUESTION_FIELDS = (
    "parent_id",
    "question_id",
    "stem",
    "options",
    "analysis",
    "sub_questions",
    "stem_pic_url",
    "analysis_pic_url",
    "image_text_supplement",
)


def jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError(f"{path}:{number} 顶层不是JSON对象")
            yield number, item


def raw_stage5_level(item: Mapping[str, Any]) -> str:
    rating = item.get("difficulty_rating")
    raw_rating = item.get("difficulty_rating_raw")
    postprocess_original = (
        str(rating.get("postprocess_original_level", "") or "").strip()
        if isinstance(rating, Mapping)
        else ""
    )
    model_raw = (
        str(raw_rating.get("difficulty_level", "") or "").strip()
        if isinstance(raw_rating, Mapping)
        else ""
    )
    if postprocess_original and model_raw and postprocess_original != model_raw:
        raise ValueError(
            "Stage5原始档位来源冲突: "
            f"postprocess_original_level={postprocess_original!r}, "
            f"difficulty_rating_raw={model_raw!r}"
        )
    level = postprocess_original or model_raw
    if level not in {"送分题", "基础题", "中等题", "拔高题", "压轴题"}:
        raise ValueError(f"缺少合法Stage5原始档位: {level!r}")
    return level


def extract_candidate(item: Mapping[str, Any]) -> dict[str, Any]:
    candidate = {
        field: item[field]
        for field in ALLOWED_QUESTION_FIELDS
        if field in item
    }
    candidate["stage52_candidate_source"] = "stage5_compact_raw_level_4"
    candidate["stage52_stage5_raw_level"] = "拔高题"
    forbidden = {
        "difficulty",
        "difficulty_rating",
        "difficulty_rating_raw",
        "features",
        "reasoning",
        "core_basis",
        "why_not_higher",
        "standard_level",
        "teacher_reason",
    }
    leaked = sorted(forbidden & set(candidate))
    if leaked:
        raise ValueError(f"B候选泄漏Stage5或教师信息: {leaked}")
    return candidate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="提取Stage5原始4档B审计候选")
    parser.add_argument("--stage5-results", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = Path(args.stage5_results).resolve()
    output = Path(args.output).resolve()
    manifest = Path(args.manifest).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    total = 0
    candidates = 0
    with output.open("w", encoding="utf-8") as target:
        for number, item in jsonl(source):
            total += 1
            question_id = str(item.get("question_id", "") or "").strip()
            if not question_id:
                raise ValueError(f"{source}:{number} 缺少question_id")
            if question_id in seen:
                raise ValueError(f"Stage5结果存在重复question_id: {question_id}")
            seen.add(question_id)
            if raw_stage5_level(item) != "拔高题":
                continue
            target.write(
                json.dumps(extract_candidate(item), ensure_ascii=False) + "\n"
            )
            candidates += 1
    manifest.write_text(
        json.dumps(
            {
                "source": str(source),
                "total_unique_questions": total,
                "raw_level_4_candidates": candidates,
                "selection_rule": (
                    "difficulty_rating.postprocess_original_level, falling back "
                    "to difficulty_rating_raw.difficulty_level; top-level "
                    "difficulty is forbidden"
                ),
                "audit_input_allowlist": list(ALLOWED_QUESTION_FIELDS),
                "stage5_reasoning_or_features_included": False,
                "teacher_label_or_reason_included": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Stage5题目数: {total}")
    print(f"原始4档B候选数: {candidates}")
    print(f"候选文件: {output}")


if __name__ == "__main__":
    main()
