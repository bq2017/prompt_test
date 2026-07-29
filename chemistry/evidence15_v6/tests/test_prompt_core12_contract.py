from __future__ import annotations

import ast
import json
import re
import sys
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from chemistry_core12_schema import CORE_FEATURE_FIELDS, CORE_FEATURE_VALUES


PROMPT = PACKAGE_ROOT / "prompts" / "0730初中化学难度打标提示词_v5_2_evidence15_v6.txt"
RUNNER = PACKAGE_ROOT / "src" / "chemistry_difficulty_rating_0730_v5_2_evidence15_v6_with_cache.py"


def load_prefix() -> str:
    namespace: dict[str, object] = {}
    exec(PROMPT.read_text(encoding="utf-8"), namespace)
    return str(namespace["DIFFICULTY_RATING_PROMPT_PREFIX"])


def json_after(text: str, marker: str) -> dict:
    start = text.index("{", text.index(marker))
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    return value


class PromptCore12ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prompt = load_prefix()

    def test_no_question_id_in_prompt(self) -> None:
        self.assertNotRegex(self.prompt, r"(?<!\d)\d{18,20}(?!\d)")
        self.assertNotRegex(self.prompt, r"\bID\b|题目ID|例题ID|教师ID")

    def test_all_levels_use_required_module_headings(self) -> None:
        for level, name in enumerate(("送分题", "基础题", "中等题", "拔高题", "压轴题"), 1):
            start = self.prompt.index(f"## 难度{level} — {name}")
            end = self.prompt.find("## 难度", start + 5)
            if end < 0:
                end = self.prompt.index("## 五、", start)
            section = self.prompt[start:end]
            for heading in ("### 核心判定依据", "### 细粒度特征", "### 代表性例题", "### 常见误判警示"):
                self.assertIn(heading, section)
            self.assertGreaterEqual(section.count("#### 教师示例："), 2)

    def test_targeted_root_causes_are_explicit(self) -> None:
        for text in (
            "纵向链深D",
            "整题任务负担B",
            "熟悉直接分类",
            "组分判断决定方程式",
            "实验设计题",
            "压轴结构是开放集合",
            "图片的整体流程",
            "绝不表示题目没有小问",
            "最小充分解题链",
            "透明映射和一步应用必须分开",
            "反应链和定量链不得拆成局部常规步骤",
            "特征—理由一致性检查",
            "entry_operation",
            "task_rule_breadth",
            "visual_information_role",
            "Evidence-15强制一致性",
        ):
            self.assertIn(text, self.prompt)

    def test_boundary_examples_follow_required_form(self) -> None:
        boundary = self.prompt.split("## 五、相邻档位边界校准 few-shot", 1)[1].split("## 六、", 1)[0]
        for heading in ("送分题 vs 基础题", "基础题 vs 中等题", "中等题 vs 拔高题", "拔高题 vs 压轴题"):
            self.assertIn(heading, boundary)
        self.assertGreaterEqual(boundary.count("【边界示例"), 13)
        for block in boundary.split("【边界示例")[1:]:
            for field in ("题目摘要：", "最终等级：", "核心特征：", "判定理由：", "为什么不更低：", "为什么不更高："):
                self.assertIn(field, block)

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
        self.assertNotIn('"question_structure"', template)
        self.assertNotIn('"subquestion_analysis"', template)
        self.assertNotIn('"confidence"', template)
        self.assertEqual(template.count('"difficulty_level"'), 1)
        for field, allowed in CORE_FEATURE_VALUES.items():
            match = re.search(rf'"{re.escape(field)}"\s*:\s*"([^"]+)"', template)
            self.assertIsNotNone(match, field)
            self.assertEqual(tuple(match.group(1).split("/")), allowed, field)

    def test_runner_has_one_postprocess_entry(self) -> None:
        tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
        entries = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "postprocess_chemistry_difficulty"
        ]
        self.assertEqual(len(entries), 1)


if __name__ == "__main__":
    unittest.main()
