from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / "prompts" / "evidence15_v9_prompt.txt"
RUNNER_PATH = ROOT / "src" / "chemistry_difficulty_rating_evidence15_v9_with_cache.py"
RUN_SCRIPT_PATH = ROOT / "tools" / "run_chemistry_evidence15_v9_stage3_teacher0724_591.sh"


def load_prompt() -> str:
    namespace: dict[str, object] = {}
    exec(PROMPT_PATH.read_text(encoding="utf-8"), namespace)
    return str(namespace["DIFFICULTY_RATING_PROMPT_PREFIX"])


class Evidence15V9Stage3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prompt = load_prompt()

    def test_each_option_internal_depth_precedes_horizontal_independence(self) -> None:
        self.assertIn("选择题必须分别检查每个选项内部的必要过程", self.prompt)
        self.assertIn("以其中最长的一条作为D", self.prompt)
        self.assertIn("选项之间独立”只说明横向关系", self.prompt)
        self.assertIn("必须先排除中等结构，再使用“独立应用束”判基础题", self.prompt)

    def test_basic_bundle_has_strict_one_step_guard(self) -> None:
        self.assertIn("只有当每项最高均为0-1层", self.prompt)
        self.assertIn("无共享建模、无完整闭环、无联合筛选", self.prompt)
        self.assertIn("独立一步应用束中的每一项都必须能在0-1层内局部完成", self.prompt)

    def test_shared_model_is_not_inferred_from_background_only(self) -> None:
        self.assertIn("生活背景或装饰图片不算", self.prompt)
        self.assertIn("同一曲线、表格、实验过程、反应后体系或流程节点", self.prompt)
        self.assertIn("选项最终各自判正误，不会消除这一结构", self.prompt)

    def test_teacher_middle_anchors_are_present(self) -> None:
        self.assertIn("选项独立但最难选项内部存在连续过程", self.prompt)
        self.assertIn("完整实验过程不能拆成独立规范", self.prompt)
        self.assertIn("粗盐中难溶性杂质去除实验", self.prompt)

    def test_other_boundaries_and_controlled_variables_are_preserved(self) -> None:
        runner = RUNNER_PATH.read_text(encoding="utf-8")
        script = RUN_SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("不得建立“某类教材原型一律送分”的题型白名单", self.prompt)
        self.assertIn("卡点路径", self.prompt)
        self.assertIn("至少两个高阶处理阶段", self.prompt)
        self.assertIn("Evidence-15 V9阶段3", runner)
        self.assertIn("evidence15_v9_stage3_591_", script)
        self.assertIn("--concurrency 30", script)
        self.assertIn("evidence15_boundary_rules_v9_stage1", script)
        self.assertIn('--labels "${LABELS}"', script)
        self.assertIn("顶层 difficulty 是旧输入错误标签", script)


if __name__ == "__main__":
    unittest.main()
