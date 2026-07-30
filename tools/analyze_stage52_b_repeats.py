"""Analyze three Stage5.2-B runs by unique question, including all non-5 errors."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from extract_stage52_b_candidates import raw_stage5_level


LEVEL_NAME_TO_NUMBER = {
    "送分题": 1,
    "基础题": 2,
    "中等题": 3,
    "拔高题": 4,
    "压轴题": 5,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage5.2-B三次唯一题目分析")
    parser.add_argument("--labels", required=True)
    parser.add_argument(
        "--run",
        action="append",
        nargs=4,
        metavar=("NAME", "STAGE5_S_JSONL", "STAGE52_T_JSONL", "RUN_MANIFEST"),
        required=True,
        help="重复三次；S读取Stage5原始档位，T读取确定性映射结果",
    )
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-csv", required=True)
    return parser.parse_args()


def load_labels(path: Path) -> dict[str, int]:
    labels: dict[str, int] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            question_id = str(row.get("question_id", "") or "").strip()
            if not question_id:
                continue
            level = int(row["standard_level"])
            if level not in range(1, 6):
                raise ValueError(f"{question_id} 教师档位非法: {level}")
            if question_id in labels:
                raise ValueError(f"教师标签重复ID: {question_id}")
            labels[question_id] = level
    return labels


def load_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            item = json.loads(line)
            question_id = str(item.get("question_id", "") or "").strip()
            if not question_id:
                raise ValueError(f"{path}:{number} 缺少question_id")
            if question_id in items:
                raise ValueError(f"{path} 重复ID: {question_id}")
            items[question_id] = item
    return items


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("protocol") != "stage52_b_frozen_stage5_independent_audit_v1":
        raise ValueError(f"{path} 不是Stage5.2-B正式运行清单")
    return manifest


def frozen_signature(manifest: dict[str, Any]) -> dict[str, Any]:
    files = manifest["files"]
    return {
        "input_data_sha256": files["input_data"]["sha256"],
        "stage5_prompt_sha256": files["stage5_prompt"]["sha256"],
        "stage5_effective_prompt_sha256": files["stage5_prompt"][
            "effective_prompt_sha256"
        ],
        "b_audit_prompt_sha256": files["b_audit_prompt"]["sha256"],
        "b_audit_effective_prompt_sha256": files["b_audit_prompt"][
            "effective_prompt_sha256"
        ],
        "candidate_extractor_sha256": files["candidate_extractor"]["sha256"],
        "deterministic_mapper_sha256": files["deterministic_mapper"]["sha256"],
        "b_audit_runner_sha256": files["b_audit_runner"]["sha256"],
        "b_schema_sha256": files["b_schema"]["sha256"],
        "model_name": manifest["model_name"],
        "temperature": manifest["temperature"],
        "concurrency": manifest["concurrency"],
        "stage5_postprocess_profile": manifest["stage5_postprocess_profile"],
        "b_schema_version": manifest["b_schema_version"],
    }


def safe_rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def predicted_t_level(item: dict[str, Any]) -> str:
    stage52 = item.get("stage52_b")
    if not isinstance(stage52, dict):
        raise ValueError("T结果缺少stage52_b")
    level = str(stage52.get("t_level", "") or "").strip()
    if level not in LEVEL_NAME_TO_NUMBER:
        raise ValueError(f"T档位非法: {level!r}")
    return level


def metric_block(labels: dict[str, int], predictions: dict[str, int]) -> dict[str, Any]:
    ids = sorted(set(labels) & set(predictions))
    exact = sum(predictions[qid] == labels[qid] for qid in ids)
    absolute = sum(abs(predictions[qid] - labels[qid]) for qid in ids)
    true4 = [qid for qid in ids if labels[qid] == 4]
    true5 = [qid for qid in ids if labels[qid] == 5]
    pred5 = [qid for qid in ids if predictions[qid] == 5]
    return {
        "evaluated": len(ids),
        "accuracy": safe_rate(exact, len(ids)),
        "mae": round(absolute / len(ids), 6) if ids else None,
        "teacher4_recall": safe_rate(
            sum(predictions[qid] == 4 for qid in true4), len(true4)
        ),
        "teacher5_recall": safe_rate(
            sum(predictions[qid] == 5 for qid in true5), len(true5)
        ),
        "predicted5_precision": safe_rate(
            sum(labels[qid] == 5 for qid in pred5), len(pred5)
        ),
        "predicted5_count": len(pred5),
    }


def promotion_block(
    labels: dict[str, int],
    s_predictions: dict[str, int],
    t_predictions: dict[str, int],
) -> tuple[dict[str, Any], set[str]]:
    promoted = {
        qid
        for qid in labels
        if s_predictions.get(qid) == 4 and t_predictions.get(qid) == 5
    }
    by_teacher = Counter(labels[qid] for qid in promoted)
    teacher5 = by_teacher[5]
    non5 = len(promoted) - teacher5
    teacher4 = by_teacher[4]
    serious = sum(by_teacher[level] for level in (1, 2, 3))
    abs_changes = [
        abs(5 - labels[qid]) - abs(4 - labels[qid])
        for qid in promoted
    ]
    return (
        {
            "b_added_count": len(promoted),
            "teacher_level_counts": {
                str(level): by_teacher[level] for level in range(1, 6)
            },
            "teacher5_rescued": teacher5,
            "all_non5_promoted": non5,
            "teacher4_to_5": teacher4,
            "teacher_le3_to_5_severe": serious,
            "b_marginal_precision": safe_rate(teacher5, len(promoted)),
            "b_protocol_net_benefit": teacher5 - non5,
            "exact_match_delta": teacher5 - teacher4,
            "average_absolute_error_change": (
                round(statistics.mean(abs_changes), 6) if abs_changes else 0.0
            ),
        },
        promoted,
    )


def aggregate_set(labels: dict[str, int], ids: set[str]) -> dict[str, Any]:
    counts = Counter(labels[qid] for qid in ids)
    teacher5 = counts[5]
    non5 = len(ids) - teacher5
    return {
        "unique_questions": len(ids),
        "teacher_level_counts": {
            str(level): counts[level] for level in range(1, 6)
        },
        "teacher5": teacher5,
        "all_non5": non5,
        "teacher4_to_5": counts[4],
        "teacher_le3_to_5_severe": sum(counts[level] for level in (1, 2, 3)),
        "marginal_precision": safe_rate(teacher5, len(ids)),
        "protocol_net_benefit": teacher5 - non5,
    }


def mean_metric(run_reports: list[dict[str, Any]], section: str, key: str) -> float:
    values = [
        report[section][key]
        for report in run_reports
        if report[section][key] is not None
    ]
    return round(statistics.mean(values), 6) if values else math.nan


def main() -> None:
    args = parse_args()
    if len(args.run) != 3:
        raise ValueError("正式Stage5.2-B实验必须且只能提供三次--run")
    labels = load_labels(Path(args.labels).resolve())
    run_reports: list[dict[str, Any]] = []
    promotion_sets: list[set[str]] = []
    per_question: dict[str, dict[str, Any]] = {
        qid: {"question_id": qid, "teacher_level": level}
        for qid, level in labels.items()
    }

    signatures: list[dict[str, Any]] = []
    for name, s_path_text, t_path_text, manifest_path_text in args.run:
        manifest = load_manifest(Path(manifest_path_text).resolve())
        if manifest.get("run_name") != name:
            raise ValueError(f"{name}: run manifest名称不一致")
        signatures.append(frozen_signature(manifest))
        s_items = load_jsonl(Path(s_path_text).resolve())
        t_items = load_jsonl(Path(t_path_text).resolve())
        if set(s_items) != set(t_items):
            raise ValueError(f"{name}: S/T题目集合不一致")
        if set(s_items) != set(labels):
            missing = sorted(set(labels) - set(s_items))
            extra = sorted(set(s_items) - set(labels))
            raise ValueError(
                f"{name}: 预测与教师标签题目集合不一致: "
                f"missing={missing[:10]}, extra={extra[:10]}"
            )
        s_predictions = {
            qid: LEVEL_NAME_TO_NUMBER[raw_stage5_level(item)]
            for qid, item in s_items.items()
        }
        t_predictions = {
            qid: LEVEL_NAME_TO_NUMBER[predicted_t_level(item)]
            for qid, item in t_items.items()
        }
        s_metrics = metric_block(labels, s_predictions)
        t_metrics = metric_block(labels, t_predictions)
        promotions, promoted_ids = promotion_block(
            labels, s_predictions, t_predictions
        )
        promotion_sets.append(promoted_ids)
        eligible = {
            qid for qid, prediction in s_predictions.items() if prediction == 4
        }
        valid_audits = sum(
            bool((t_items[qid].get("stage52_b") or {}).get("audit_available"))
            for qid in eligible
        )
        coverage = safe_rate(valid_audits, len(eligible))
        l4_drop = (
            round(s_metrics["teacher4_recall"] - t_metrics["teacher4_recall"], 6)
            if s_metrics["teacher4_recall"] is not None
            and t_metrics["teacher4_recall"] is not None
            else None
        )
        report = {
            "name": name,
            "s": s_metrics,
            "t": t_metrics,
            "delta": {
                "accuracy": round(t_metrics["accuracy"] - s_metrics["accuracy"], 6),
                "mae": round(t_metrics["mae"] - s_metrics["mae"], 6),
                "teacher4_recall_drop": l4_drop,
                "teacher5_recall_gain": round(
                    t_metrics["teacher5_recall"] - s_metrics["teacher5_recall"],
                    6,
                ),
            },
            "b": {
                **promotions,
                "eligible_raw_level_4": len(eligible),
                "valid_audit_coverage": coverage,
            },
        }
        run_reports.append(report)
        for qid in labels:
            per_question[qid][f"{name}_s"] = s_predictions.get(qid)
            per_question[qid][f"{name}_t"] = t_predictions.get(qid)
            per_question[qid][f"{name}_b_added"] = qid in promoted_ids

    if any(signature != signatures[0] for signature in signatures[1:]):
        raise ValueError("三次run manifest冻结条件不一致，禁止汇总")

    trigger_counts = {
        qid: sum(qid in promoted for promoted in promotion_sets)
        for qid in labels
    }
    any_added = {qid for qid, count in trigger_counts.items() if count >= 1}
    majority_added = {qid for qid, count in trigger_counts.items() if count >= 2}
    stable_added = {qid for qid, count in trigger_counts.items() if count == 3}
    for qid, count in trigger_counts.items():
        per_question[qid]["b_added_run_count"] = count

    majority = aggregate_set(labels, majority_added)
    stable = aggregate_set(labels, stable_added)
    mechanism_checks = {
        "each_run_protocol_net_positive": all(
            report["b"]["b_protocol_net_benefit"] > 0 for report in run_reports
        ),
        "majority_marginal_precision_ge_70pct": (
            majority["marginal_precision"] is not None
            and majority["marginal_precision"] >= 0.70
        ),
        "each_run_audit_coverage_ge_98pct": all(
            (report["b"]["valid_audit_coverage"] or 0) >= 0.98
            for report in run_reports
        ),
        "each_run_teacher4_recall_drop_le_3pp": all(
            (report["delta"]["teacher4_recall_drop"] or 0) <= 0.03
            for report in run_reports
        ),
        "stable_rescues_exceed_stable_all_non5_promotions": (
            stable["teacher5"] > stable["all_non5"]
        ),
    }
    mechanism_pass = all(mechanism_checks.values())
    candidate_checks = {
        "mean_teacher5_recall_ge_30pct": (
            mean_metric(run_reports, "t", "teacher5_recall") >= 0.30
        ),
        "mean_predicted5_precision_ge_65pct": (
            mean_metric(run_reports, "t", "predicted5_precision") >= 0.65
        ),
        "t_accuracy_higher_each_run": all(
            report["delta"]["accuracy"] > 0 for report in run_reports
        ),
        "t_mae_not_higher_each_run": all(
            report["delta"]["mae"] <= 0 for report in run_reports
        ),
    }
    candidate_pass = all(candidate_checks.values())
    stage4_benchmark = {
        "raw_three_run_mean_accuracy": 0.615905,
        "raw_three_run_mean_mae": 0.408023,
        "mean_t_accuracy_meets_stage4": (
            mean_metric(run_reports, "t", "accuracy") >= 0.615905
        ),
        "mean_t_mae_meets_stage4": (
            mean_metric(run_reports, "t", "mae") <= 0.408023
        ),
    }

    summary = {
        "protocol": "stage52_b_frozen_stage5_independent_audit_v1",
        "standard_label_counts": {
            str(level): sum(value == level for value in labels.values())
            for level in range(1, 6)
        },
        "runs": run_reports,
        "frozen_run_signature": signatures[0],
        "unique_question_aggregation": {
            "any_added": aggregate_set(labels, any_added),
            "majority_added_primary": majority,
            "stable_added": stable,
        },
        "mechanism_acceptance": {
            "checks": mechanism_checks,
            "passed": mechanism_pass,
        },
        "stage52_candidate_acceptance": {
            "checks": candidate_checks,
            "passed": candidate_pass,
        },
        "stage4_comparison": stage4_benchmark,
        "interpretation_rule": (
            "B机制通过与Stage5.2整体候选通过分别判断；protocol net benefit "
            "= 新增教师5 - 新增所有非5。"
        ),
    }

    output_json = Path(args.output_json).resolve()
    output_csv = Path(args.output_csv).resolve()
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    fieldnames = list(next(iter(per_question.values())).keys())
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(per_question.values())
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
