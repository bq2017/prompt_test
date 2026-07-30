"""Apply the deterministic Stage5.2-B 4→5 mapping to frozen Stage5 output."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chemistry_stage52_b_schema import (
    SCHEMA_VERSION,
    derive_composite_burden,
    failed_gates,
    validate_b_audit,
)
from extract_stage52_b_candidates import raw_stage5_level


def jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError(f"{path}:{number} 顶层不是JSON对象")
            yield item


def load_audits(path: Path) -> dict[str, dict[str, Any]]:
    audits: dict[str, dict[str, Any]] = {}
    for item in jsonl(path):
        question_id = str(item.get("question_id", "") or "").strip()
        if not question_id:
            raise ValueError(f"{path} 存在缺少question_id的审计")
        if question_id in audits:
            raise ValueError(f"{path} 存在重复B审计: {question_id}")
        if item.get("stage52_b_schema_version") != SCHEMA_VERSION:
            raise ValueError(f"{question_id} B schema版本不匹配")
        audit = validate_b_audit(item.get("stage52_b_audit"))
        stored_trigger = item.get("stage52_b_trigger")
        derived_trigger = derive_composite_burden(audit)
        if type(stored_trigger) is not bool or stored_trigger != derived_trigger:
            raise ValueError(f"{question_id} B触发值未由schema确定性导出")
        audits[question_id] = audit
    return audits


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="确定性生成Stage5.2-T结果")
    parser.add_argument("--stage5-results", required=True)
    parser.add_argument("--b-audits", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = Path(args.stage5_results).resolve()
    audit_path = Path(args.b_audits).resolve()
    output = Path(args.output).resolve()
    summary_path = Path(args.summary).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    audits = load_audits(audit_path)

    seen: set[str] = set()
    eligible: set[str] = set()
    used_audits: set[str] = set()
    promoted = 0
    missing_audits: list[str] = []
    total = 0

    with output.open("w", encoding="utf-8") as target:
        for item in jsonl(source):
            total += 1
            question_id = str(item.get("question_id", "") or "").strip()
            if not question_id:
                raise ValueError("Stage5结果存在缺少question_id的题")
            if question_id in seen:
                raise ValueError(f"Stage5结果存在重复question_id: {question_id}")
            seen.add(question_id)
            raw_level = raw_stage5_level(item)
            audit = None
            trigger = False
            if raw_level == "拔高题":
                eligible.add(question_id)
                audit = audits.get(question_id)
                if audit is None:
                    missing_audits.append(question_id)
                else:
                    used_audits.add(question_id)
                    trigger = derive_composite_burden(audit)
            elif question_id in audits:
                raise ValueError(f"非原始4档题出现B审计: {question_id}")

            mapped = copy.deepcopy(item)
            rating = mapped.get("difficulty_rating")
            if not isinstance(rating, dict):
                raise ValueError(f"{question_id} 缺少difficulty_rating")
            t_level = "压轴题" if trigger else raw_level
            if trigger:
                promoted += 1
            rating["difficulty_level"] = t_level
            rating["postprocess_original_level"] = raw_level
            rating["coarse_difficulty"] = (
                "拔高/压轴区间（4-5档）"
                if t_level in {"拔高题", "压轴题"}
                else rating.get("coarse_difficulty")
            )
            rating["automatic_level_change_applied"] = False
            rating["postprocess_profile"] = (
                "evidence15_boundary_rules_v9_stage5_audit45"
            )
            rating["postprocess_actions"] = []
            rating["postprocess_trace"] = []
            mapped["stage52_b"] = {
                "schema_version": SCHEMA_VERSION,
                "eligible_raw_stage5_level_4": raw_level == "拔高题",
                "audit_available": audit is not None,
                "trigger": trigger,
                "failed_gates": failed_gates(audit) if audit is not None else [],
                "s_level": raw_level,
                "t_level": t_level,
                "mapping": "deterministic_4_to_5_only",
                "stage5_reasoning_passed_to_auditor": False,
                "teacher_information_passed_to_auditor": False,
            }
            if audit is not None:
                mapped["stage52_b"]["audit"] = audit
            target.write(json.dumps(mapped, ensure_ascii=False) + "\n")

    extra_audits = sorted(set(audits) - eligible)
    if extra_audits:
        raise ValueError(f"B审计包含非候选题: {extra_audits[:10]}")
    coverage = len(used_audits) / len(eligible) if eligible else 1.0
    summary = {
        "protocol": "stage52_b_frozen_stage5_independent_audit_v1",
        "stage5_results": str(source),
        "b_audits": str(audit_path),
        "total_questions": total,
        "eligible_raw_level_4": len(eligible),
        "valid_b_audits": len(used_audits),
        "audit_coverage": round(coverage, 6),
        "missing_audit_count": len(missing_audits),
        "missing_audit_ids": sorted(missing_audits),
        "b_promotions": promoted,
        "postprocess_4_to_5": "audit_only_not_applied",
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
