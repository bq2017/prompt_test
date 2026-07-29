from __future__ import annotations

import ast
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chemistry_evidence15_v9_schema import CORE_FEATURE_FIELDS, CORE_FEATURE_VALUES  # noqa: E402


PROMPT = ROOT / "prompts" / "evidence15_v9_prompt.txt"
RUNNER = ROOT / "src" / "chemistry_difficulty_rating_evidence15_v9_with_cache.py"


def load_prefix() -> str:
    namespace: dict[str, object] = {}
    exec(PROMPT.read_text(encoding="utf-8"), namespace)
    return str(namespace["DIFFICULTY_RATING_PROMPT_PREFIX"])


def json_after(text: str, marker: str) -> dict:
    start = text.index("{", text.index(marker))
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    return value


class PromptEvidence15V9ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prompt = load_prefix()

    def test_v9_stage1_uses_six_steps_and_rates_before_features(self) -> None:
        self.assertIn("## 一、统一的六步处理流程", self.prompt)
        self.assertIn("### 第三步：核对真实解题任务", self.prompt)
        rating_index = self.prompt.index("### 第四步：五维初判并完成相邻定档")
        feature_index = self.prompt.index("### 第五步：填写15项难度审计特征")
        self.assertLess(rating_index, feature_index)
        self.assertIn("最终档位确定后", self.prompt[feature_index:])

    def test_obsolete_structure_terms_are_absent(self) -> None:
        for text in (
            "任务图",
            "任务边",
            "三点五",
            "12个核心特征",
            "12项核心特征",
            "统一的七步处理流程",
        ):
            self.assertNotIn(text, self.prompt)

    def test_continuous_dependency_has_two_explicit_conditions(self) -> None:
        self.assertIn("前一步产生题面未直接给出的新结论", self.prompt)
        self.assertIn("后一步必须使用该结论", self.prompt)
        self.assertIn("若后一步不需要前一步结论也能独立完成，则不属于连续依赖", self.prompt)

    def test_all_15_features_are_continuously_numbered(self) -> None:
        self.assertEqual(len(CORE_FEATURE_FIELDS), 15)
        for index, field in enumerate(CORE_FEATURE_FIELDS, 1):
            self.assertIn(f"### {index}. {field}", self.prompt)

    def test_removed_cross_field_hard_bindings_are_absent(self) -> None:
        self.assertNotIn("自主选择一条规则`时，`reasoning_depth`必须为`1层", self.prompt)
        self.assertNotIn("任务链依赖在task_rule_breadth与subquestion_dependency中必须一致", self.prompt)
        self.assertIn("entry_operation`只描述最高难任务开始时的第一个实质操作", self.prompt)
        self.assertIn("subquestion_dependency`只描述跨小问关系", self.prompt)

    def test_no_question_id_in_prompt(self) -> None:
        self.assertNotRegex(self.prompt, r"(?<!\d)\d{18,20}(?!\d)")
        self.assertNotRegex(self.prompt, r"\bID\b|题目ID|例题ID|教师ID")

    def test_legal_json_example_is_compact_and_parseable(self) -> None:
        example = json_after(self.prompt, "合法JSON示例：")
        self.assertEqual(set(example), {"features", "coarse_difficulty", "reasoning", "difficulty_level"})
        self.assertEqual(tuple(example["features"]), CORE_FEATURE_FIELDS)
        for field, allowed in CORE_FEATURE_VALUES.items():
            self.assertIn(example["features"][field], allowed)
        self.assertEqual(
            set(example["reasoning"]),
            {"core_basis", "hard_point", "why_not_lower", "why_not_higher"},
        )

    def test_fixed_template_enums_exactly_match_schema(self) -> None:
        template = self.prompt.split("## 九、固定生产JSON", 1)[1]
        self.assertEqual(template.count('"difficulty_level"'), 1)
        for field, allowed in CORE_FEATURE_VALUES.items():
            match = re.search(rf'"{re.escape(field)}"\s*:\s*"([^"]+)"', template)
            self.assertIsNotNone(match, field)
            self.assertEqual(tuple(match.group(1).split("/")), allowed, field)

    def test_runner_contract_and_default_concurrency(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        tree = ast.parse(source)
        entries = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "postprocess_chemistry_difficulty"
        ]
        self.assertEqual(len(entries), 1)
        self.assertIn('PROMPT_FILENAME = "evidence15_v9_prompt.txt"', source)
        self.assertRegex(source, r'--concurrency"\s*,\s*type=int\s*,\s*default=30')


if __name__ == "__main__":
    unittest.main()
