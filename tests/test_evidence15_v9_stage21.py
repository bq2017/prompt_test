from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chemistry_evidence15_v9_schema import CORE_FEATURE_FIELDS  # noqa: E402


PROMPT_PATH = ROOT / "prompts" / "evidence15_v9_prompt.txt"
RUNNER_PATH = ROOT / "src" / "chemistry_difficulty_rating_evidence15_v9_with_cache.py"
RUN_SCRIPT_PATH = ROOT / "tools" / "run_chemistry_evidence15_v9_stage21_teacher0724_591.sh"


def load_prompt() -> str:
    namespace: dict[str, object] = {}
    exec(PROMPT_PATH.read_text(encoding="utf-8"), namespace)
    return str(namespace["DIFFICULTY_RATING_PROMPT_PREFIX"])


class Evidence15V9Stage21Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prompt = load_prompt()

    def test_low_boundary_has_two_operational_paths(self) -> None:
        self.assertIn("实际应用路径", self.prompt)
        self.assertIn("竞争辨析路径", self.prompt)
        self.assertIn("由已给pH数值直接识别酸碱性", self.prompt)
        self.assertIn("指示剂变化、实验现象或用途并要求推出", self.prompt)
        self.assertIn("易混标准→干扰项→排除依据", self.prompt)

    def test_middle_boundary_has_three_paths_and_precedence(self) -> None:
        self.assertIn("连续依赖、完整闭环和综合核验三条通道", self.prompt)
        self.assertIn("答案不直接复用不等于任务无关", self.prompt)
        self.assertIn("不同实质任务共同完成同一实验、流程、图表、定量对象或最终选择", self.prompt)

    def test_hard_boundary_allows_cardpoint_or_integrated_mainline(self) -> None:
        self.assertIn("卡点路径", self.prompt)
        self.assertIn("综合主线路径", self.prompt)
        self.assertIn("不依赖有效决策数量", self.prompt)
        self.assertIn("单一高阶卡点或一条综合主线", self.prompt)

    def test_final_boundary_requires_cross_stage_model_effect(self) -> None:
        self.assertIn("压轴题不以步骤数量为门槛", self.prompt)
        self.assertIn("至少两个高阶处理阶段", self.prompt)
        self.assertIn("具体改变了后一阶段的哪个方程、反应模型、数据段、有效解范围、误差处理或最终计算", self.prompt)

    def test_obsolete_numeric_level_gates_are_absent(self) -> None:
        for text in (
            "约5—6个非重复有效决策",
            "实际5—6个有效决策",
            "通常有6个及以上实际有效化学决策",
            "实际3—4个有效决策",
        ):
            self.assertNotIn(text, self.prompt)

    def test_summary_self_check_and_output_template_use_same_vocabulary(self) -> None:
        self.assertIn("连续依赖、完整闭环或综合核验", self.prompt)
        self.assertIn("一个具体高阶卡点，或写清不同实质任务如何围绕同一题目模型形成综合主线", self.prompt)
        self.assertIn("实际应用或竞争辨析、常规综合、单一高阶卡点、综合主线或跨阶段联合模型", self.prompt)

    def test_schema_fields_and_postprocess_profile_are_unchanged(self) -> None:
        self.assertEqual(len(CORE_FEATURE_FIELDS), 15)
        source = RUNNER_PATH.read_text(encoding="utf-8")
        script = RUN_SCRIPT_PATH.read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn('"evidence15_boundary_rules_v9_stage1"', source)
        self.assertIn('export CHEMISTRY_EVIDENCE15_V9_POSTPROCESS_PROFILE="evidence15_boundary_rules_v9_stage1"', script)
        self.assertIn("--concurrency 30", script)

    def test_stage21_version_is_visible_in_runner_and_script(self) -> None:
        source = RUNNER_PATH.read_text(encoding="utf-8")
        script = RUN_SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("Evidence-15 V9阶段2.1", source)
        self.assertIn("evidence15_v9_stage2_1_591_", script)


if __name__ == "__main__":
    unittest.main()
