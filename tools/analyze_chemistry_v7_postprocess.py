"""Summarize V7 raw-to-conservative postprocessing behavior."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--changes", required=True)
    args = parser.parse_args()

    total = 0
    changed = 0
    candidate_questions = 0
    schema_flag_questions = 0
    raw_counts: Counter[str] = Counter()
    final_counts: Counter[str] = Counter()
    rule_counts: Counter[str] = Counter()
    candidate_counts: Counter[str] = Counter()
    schema_flag_counts: Counter[str] = Counter()
    rows: list[dict[str, object]] = []

    with open(args.input, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            rating = item.get("difficulty_rating") or {}
            raw = rating.get("postprocess_original_level") or rating.get("difficulty_level_raw")
            final = rating.get("difficulty_level")
            if not raw or not final:
                continue
            total += 1
            raw_counts[str(raw)] += 1
            final_counts[str(final)] += 1
            actions = rating.get("postprocess_actions") or []
            candidates = rating.get("postprocess_candidate_flags") or []
            schema_flags = rating.get("schema_audit_flags") or []
            if candidates:
                candidate_questions += 1
            if schema_flags:
                schema_flag_questions += 1
            for candidate in candidates:
                candidate_counts[str(candidate.get("rule", "unknown"))] += 1
            for flag in schema_flags:
                schema_flag_counts[str(flag)] += 1
            if raw != final:
                changed += 1
                action = actions[0] if actions else {}
                rule = str(action.get("rule", "missing_action"))
                rule_counts[rule] += 1
                rows.append({
                    "question_id": item.get("question_id", ""),
                    "raw_level": raw,
                    "final_level": final,
                    "rule": rule,
                    "evidence": "；".join(str(x) for x in action.get("evidence", []) or []),
                    "candidate_rule_count": len(candidates),
                    "schema_audit_flag_count": len(schema_flags),
                })

    report = {
        "total_valid_results": total,
        "postprocess_profile": "conservative_v1",
        "automatic_level_changes": changed,
        "unchanged_results": total - changed,
        "candidate_flag_questions": candidate_questions,
        "schema_audit_flag_questions": schema_flag_questions,
        "raw_level_counts": dict(raw_counts),
        "final_level_counts": dict(final_counts),
        "applied_rule_counts": dict(rule_counts),
        "candidate_rule_counts": dict(candidate_counts),
        "schema_audit_flag_counts": dict(schema_flag_counts),
    }
    report_path = Path(args.report)
    changes_path = Path(args.changes)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    changes_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "question_id", "raw_level", "final_level", "rule", "evidence",
        "candidate_rule_count", "schema_audit_flag_count",
    ]
    with changes_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"V7后处理报告: {report_path}")
    print(f"V7实际改档题目: {changes_path}")


if __name__ == "__main__":
    main()
