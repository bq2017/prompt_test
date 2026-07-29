"""Summarize V8 observe-only postprocessing and export flagged questions."""

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
    parser.add_argument("--flags", required=True)
    args = parser.parse_args()

    total = 0
    changed = 0
    flagged_questions = 0
    raw_counts: Counter[str] = Counter()
    final_counts: Counter[str] = Counter()
    flag_counts: Counter[str] = Counter()
    rows: list[dict[str, str | int]] = []

    with open(args.input, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            rating = item.get("difficulty_rating") or {}
            raw_level = rating.get("postprocess_original_level")
            final_level = rating.get("difficulty_level")
            if not raw_level or not final_level:
                continue
            total += 1
            raw_counts[str(raw_level)] += 1
            final_counts[str(final_level)] += 1
            if raw_level != final_level:
                changed += 1
            flags = rating.get("v8_audit_flags") or []
            if flags:
                flagged_questions += 1
            for flag in flags:
                code = str(flag.get("code", "unknown"))
                reason = str(flag.get("reason", ""))
                flag_counts[code] += 1
                rows.append({
                    "question_id": item.get("question_id", ""),
                    "raw_level": raw_level,
                    "final_level": final_level,
                    "flag_code": code,
                    "flag_reason": reason,
                    "node_count": rating.get("derived_taskgraph_metrics", {}).get("node_count", ""),
                    "dependency_edge_count": rating.get("derived_taskgraph_metrics", {}).get("dependency_edge_count", ""),
                    "shared_model_count": rating.get("derived_taskgraph_metrics", {}).get("shared_model_count", ""),
                    "longest_dependency_path_edges": rating.get("derived_taskgraph_metrics", {}).get("longest_dependency_path_edges", ""),
                })

    report = {
        "total_valid_results": total,
        "postprocess_profile": "v8_full_taskgraph_observe_only_v1",
        "automatic_level_changes": changed,
        "raw_final_levels_identical": changed == 0,
        "flagged_questions": flagged_questions,
        "raw_level_counts": dict(raw_counts),
        "final_level_counts": dict(final_counts),
        "audit_flag_counts": dict(flag_counts),
        "compatibility_conclusion": (
            "V8字段经专用Schema兼容当前observe-only策略；不兼容Evidence/Core-12自动改档规则。"
        ),
    }
    report_path = Path(args.report)
    flags_path = Path(args.flags)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    flags_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    fieldnames = [
        "question_id", "raw_level", "final_level", "flag_code", "flag_reason",
        "node_count", "dependency_edge_count", "shared_model_count",
        "longest_dependency_path_edges",
    ]
    with flags_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"V8后处理报告: {report_path}")
    print(f"V8审计旗标题目: {flags_path}")


if __name__ == "__main__":
    main()
