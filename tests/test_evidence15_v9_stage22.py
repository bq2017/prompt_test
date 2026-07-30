from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import evaluate_chemistry_difficulty as evaluator  # noqa: E402


PROMPT_PATH = ROOT / "prompts" / "evidence15_v9_prompt.txt"
RUNNER_PATH = ROOT / "src" / "chemistry_difficulty_rating_evidence15_v9_with_cache.py"
RUN_SCRIPT_PATH = ROOT / "tools" / "run_chemistry_evidence15_v9_stage22_teacher0724_591.sh"


def load_prompt() -> str:
    namespace: dict[str, object] = {}
    exec(PROMPT_PATH.read_text(encoding="utf-8"), namespace)
    return str(namespace["DIFFICULTY_RATING_PROMPT_PREFIX"])


class Evidence15V9Stage22Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prompt = load_prompt()

    def test_textbook_prototypes_remain_level_one_candidates(self) -> None:
        for text in (
            "物理变化/化学变化",
            "缓慢氧化",
            "常见物质或材料类别",
            "常见化学式、离子符号和化学用语正误",
            "常见实验安全规范",
            "低碳行为",
            "教材原型识别延展性",
        ):
            self.assertIn(text, self.prompt)
        self.assertIn("根据定义、应用规则、逐项判断", self.prompt)
        self.assertIn("仍不能自动升档", self.prompt)

    def test_level_two_requires_new_information_or_real_competition(self) -> None:
        self.assertIn("新增信息→规律→新增结论", self.prompt)
        self.assertIn("新增结论不能只是给熟悉实例贴上教材类别标签", self.prompt)
        self.assertIn("竞争解释A/B→决定性差异→排除结果", self.prompt)
        self.assertIn("普通选择题排除错误项", self.prompt)

    def test_effective_high_level_boundaries_are_preserved(self) -> None:
        self.assertIn("卡点路径", self.prompt)
        self.assertIn("综合主线路径", self.prompt)
        self.assertIn("至少两个高阶处理阶段", self.prompt)
        self.assertIn("前一阶段的结果会改变后一阶段", self.prompt)

    def test_evaluator_ignores_top_level_difficulty_and_uses_teacher_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            labels = directory / "labels.csv"
            predictions = directory / "predictions.jsonl"
            report = directory / "evaluation.json"
            mismatches = directory / "mismatches.csv"
            labels.write_text(
                "question_id,standard_stars,standard_level,standard_level_name,reason\n"
                "q1,1,1,送分题,教师清洗标签\n",
                encoding="utf-8-sig",
            )
            predictions.write_text(
                json.dumps(
                    {
                        "question_id": "q1",
                        "difficulty": 5,
                        "difficulty_rating": {"difficulty_level": "送分题"},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            argv = [
                "evaluate_chemistry_difficulty.py",
                "--labels", str(labels),
                "--predictions", str(predictions),
                "--report", str(report),
                "--mismatches", str(mismatches),
            ]
            with patch.object(sys, "argv", argv):
                evaluator.main()
            result = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(result["exact_matches"], 1)
            self.assertEqual(result["standard_label_source"], "teacher_clean_csv.standard_level")
            self.assertEqual(
                result["prediction_jsonl_top_level_difficulty_policy"],
                "ignored_stale_input_label",
            )
            self.assertFalse(result["prediction_jsonl_top_level_difficulty_used_for_scoring"])

    def test_stage22_runner_and_script_keep_controlled_variables(self) -> None:
        runner = RUNNER_PATH.read_text(encoding="utf-8")
        script = RUN_SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("Evidence-15 V9阶段2.2", runner)
        self.assertIn("evidence15_v9_stage2_2_591_", script)
        self.assertIn("--concurrency 30", script)
        self.assertIn("evidence15_boundary_rules_v9_stage1", script)
        self.assertIn("--labels \"${LABELS}\"", script)


if __name__ == "__main__":
    unittest.main()
