#!/usr/bin/env python3
"""Evaluate predictions only against teacher-cleaned CSV labels by question ID.

The top-level ``difficulty`` field in prediction JSONL files is a stale input
label. It is not the teacher-cleaned label and must never be used for scoring.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


LEVEL_NAME_TO_NUMBER = {
    "送分题": 1,
    "基础题": 2,
    "中等题": 3,
    "拔高题": 4,
    "压轴题": 5,
}
LEVEL_NUMBER_TO_NAME = {value: key for key, value in LEVEL_NAME_TO_NUMBER.items()}
STANDARD_LABEL_SOURCE = "teacher_clean_csv.standard_level"
TOP_LEVEL_DIFFICULTY_POLICY = "ignored_stale_input_label"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按 question_id 评测化学难度预测")
    parser.add_argument("--labels", required=True, help="prepare 脚本生成的干净标签 CSV")
    parser.add_argument("--predictions", required=True, help="化学评级脚本生成的 JSONL")
    parser.add_argument("--errors", help="可选：化学评级错误 JSONL")
    parser.add_argument(
        "--level-source",
        choices=["final", "pre-postprocess"],
        default="final",
        help="final=评测后处理最终档位；pre-postprocess=优先评测后处理前的模型原始档位",
    )
    parser.add_argument("--report", required=True, help="JSON 指标报告输出路径")
    parser.add_argument("--mismatches", required=True, help="错题明细 CSV 输出路径")
    return parser.parse_args()


def load_labels(path: Path) -> dict[str, dict[str, Any]]:
    labels: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            question_id = str(row.get("question_id", "")).strip()
            if not question_id:
                continue
            level = int(row["standard_level"])
            if level not in LEVEL_NUMBER_TO_NAME:
                raise ValueError(f"ID={question_id} 的标准等级非法: {level}")
            if question_id in labels:
                raise ValueError(f"干净标签中仍有重复 question_id: {question_id}")
            labels[question_id] = {
                "standard_stars": row.get("standard_stars", ""),
                "standard_level": level,
                "standard_level_name": row.get("standard_level_name", LEVEL_NUMBER_TO_NAME[level]),
                "reason": row.get("reason", ""),
            }
    return labels


def jsonl_items(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path} 第 {line_number} 行不是合法 JSON: {exc}") from exc
            if isinstance(item, dict):
                yield line_number, item


def extract_prediction(
    item: dict[str, Any], level_source: str
) -> tuple[str | None, int | None]:
    rating = item.get("difficulty_rating")
    level_name = None
    if isinstance(rating, dict):
        if level_source == "pre-postprocess":
            level_name = rating.get("postprocess_original_level")
        level_name = level_name or rating.get("difficulty_level")
    if level_name is None:
        level_name = item.get("difficulty_level")
    level_name = str(level_name).strip() if level_name is not None else None
    return level_name, LEVEL_NAME_TO_NUMBER.get(level_name) if level_name else None


def load_predictions(
    path: Path, level_source: str
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    predictions: dict[str, dict[str, Any]] = {}
    duplicate_ids: list[str] = []
    for line_number, item in jsonl_items(path):
        question_id = str(item.get("question_id", "")).strip()
        if not question_id:
            raise ValueError(f"{path} 第 {line_number} 行缺少 question_id")
        if question_id in predictions:
            duplicate_ids.append(question_id)
            continue
        level_name, level_number = extract_prediction(item, level_source)
        predictions[question_id] = {
            "predicted_level_name": level_name,
            "predicted_level": level_number,
            "stem": str(item.get("stem", "") or ""),
            "api_time_use": item.get("api_time_use"),
            "postprocess_original_level": (
                item.get("difficulty_rating", {}).get("postprocess_original_level")
                if isinstance(item.get("difficulty_rating"), dict)
                else None
            ),
            "postprocess_trace": (
                item.get("difficulty_rating", {}).get("postprocess_trace", [])
                if isinstance(item.get("difficulty_rating"), dict)
                else []
            ),
        }
    return predictions, sorted(set(duplicate_ids))


def load_error_ids(path: Path | None) -> tuple[set[str], dict[str, str]]:
    if path is None or not path.exists():
        return set(), {}
    ids: set[str] = set()
    messages: dict[str, str] = {}
    for _, item in jsonl_items(path):
        question_id = str(item.get("question_id", "")).strip()
        if question_id:
            ids.add(question_id)
            messages[question_id] = str(item.get("rating_error", ""))
    return ids, messages


def safe_rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "question_id",
        "status",
        "standard_stars",
        "standard_level",
        "standard_level_name",
        "predicted_level",
        "predicted_level_name",
        "absolute_error",
        "standard_reason",
        "stem",
        "postprocess_original_level",
        "postprocess_trace",
        "rating_error",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    labels_path = Path(args.labels).expanduser().resolve()
    predictions_path = Path(args.predictions).expanduser().resolve()
    errors_path = Path(args.errors).expanduser().resolve() if args.errors else None
    report_path = Path(args.report).expanduser().resolve()
    mismatches_path = Path(args.mismatches).expanduser().resolve()

    if not predictions_path.exists():
        raise FileNotFoundError(f"预测文件不存在: {predictions_path}")

    labels = load_labels(labels_path)
    predictions, duplicate_prediction_ids = load_predictions(
        predictions_path, args.level_source
    )
    error_ids, error_messages = load_error_ids(errors_path)

    attempted_ids = set(predictions) | error_ids
    evaluable_attempted_ids = sorted(attempted_ids & set(labels))
    prediction_ids_without_clean_label = sorted(set(predictions) - set(labels))
    legal_prediction_ids = [
        question_id
        for question_id in evaluable_attempted_ids
        if predictions.get(question_id, {}).get("predicted_level") in LEVEL_NUMBER_TO_NAME
    ]

    exact = 0
    within_one = 0
    absolute_error_sum = 0
    confusion: Counter[tuple[int, int]] = Counter()
    per_standard_total: Counter[int] = Counter()
    per_standard_correct: Counter[int] = Counter()
    mismatch_rows: list[dict[str, Any]] = []

    for question_id in evaluable_attempted_ids:
        label = labels[question_id]
        prediction = predictions.get(question_id, {})
        standard_level = label["standard_level"]
        predicted_level = prediction.get("predicted_level")
        per_standard_total[standard_level] += 1

        if predicted_level in LEVEL_NUMBER_TO_NAME:
            difference = abs(predicted_level - standard_level)
            confusion[(standard_level, predicted_level)] += 1
            absolute_error_sum += difference
            if difference == 0:
                exact += 1
                per_standard_correct[standard_level] += 1
            if difference <= 1:
                within_one += 1
            status = "correct" if difference == 0 else "mismatch"
        else:
            difference = None
            status = "request_error" if question_id in error_ids else "invalid_prediction"

        if status != "correct":
            mismatch_rows.append(
                {
                    "question_id": question_id,
                    "status": status,
                    "standard_stars": label["standard_stars"],
                    "standard_level": standard_level,
                    "standard_level_name": label["standard_level_name"],
                    "predicted_level": predicted_level if predicted_level is not None else "",
                    "predicted_level_name": prediction.get("predicted_level_name") or "",
                    "absolute_error": difference if difference is not None else "",
                    "standard_reason": label["reason"],
                    "stem": prediction.get("stem", ""),
                    "postprocess_original_level": prediction.get("postprocess_original_level") or "",
                    "postprocess_trace": json.dumps(
                        prediction.get("postprocess_trace", []), ensure_ascii=False
                    ),
                    "rating_error": error_messages.get(question_id, ""),
                }
            )

    legal_count = len(legal_prediction_ids)
    attempted_count = len(evaluable_attempted_ids)
    per_level = {}
    for level in range(1, 6):
        total = per_standard_total[level]
        correct = per_standard_correct[level]
        per_level[str(level)] = {
            "level_name": LEVEL_NUMBER_TO_NAME[level],
            "attempted": total,
            "correct": correct,
            "accuracy": safe_rate(correct, total),
        }

    confusion_matrix = {
        str(actual): {
            str(predicted): confusion[(actual, predicted)] for predicted in range(1, 6)
        }
        for actual in range(1, 6)
    }
    report = {
        "labels_file": str(labels_path),
        "standard_label_source": STANDARD_LABEL_SOURCE,
        "prediction_jsonl_top_level_difficulty_policy": TOP_LEVEL_DIFFICULTY_POLICY,
        "prediction_jsonl_top_level_difficulty_used_for_scoring": False,
        "predictions_file": str(predictions_path),
        "errors_file": str(errors_path) if errors_path else None,
        "level_source": args.level_source,
        "clean_label_ids": len(labels),
        "prediction_unique_ids": len(predictions),
        "error_unique_ids": len(error_ids),
        "attempted_unique_ids": len(attempted_ids),
        "evaluable_attempted_ids": attempted_count,
        "legal_prediction_ids": legal_count,
        "exact_matches": exact,
        "accuracy_on_legal_predictions": safe_rate(exact, legal_count),
        "strict_accuracy": safe_rate(exact, attempted_count),
        "coverage_within_attempted": safe_rate(legal_count, attempted_count),
        "within_one_level_rate": safe_rate(within_one, legal_count),
        "mae": round(absolute_error_sum / legal_count, 6) if legal_count else None,
        "prediction_ids_without_clean_label": prediction_ids_without_clean_label,
        "duplicate_prediction_ids": duplicate_prediction_ids,
        "per_standard_level": per_level,
        "confusion_matrix": confusion_matrix,
        "mismatch_count": len(mismatch_rows),
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    write_csv(mismatches_path, mismatch_rows)

    print("标准标签来源: 教师清洗CSV的 standard_level")
    print("重要: 预测JSONL顶层 difficulty 是旧输入错误标签，评测已明确忽略")
    print(f"本次模型输出唯一 ID: {len(predictions)}")
    print(f"其中有干净标准标签: {attempted_count}")
    print(f"合法难度预测: {legal_count}")
    print(f"完全一致: {exact}")
    print(f"Accuracy（合法预测）: {report['accuracy_on_legal_predictions']}")
    print(f"Strict Accuracy（失败也计错）: {report['strict_accuracy']}")
    print(f"相差不超过一档: {report['within_one_level_rate']}")
    print(f"MAE: {report['mae']}")
    print(f"无干净标准标签的预测: {len(prediction_ids_without_clean_label)}")
    print(f"报告: {report_path}")
    print(f"错题: {mismatches_path}")


if __name__ == "__main__":
    main()
