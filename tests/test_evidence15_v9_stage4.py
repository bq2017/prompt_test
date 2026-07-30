from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STAGE3_PROMPT_PATH = ROOT / "prompts" / "evidence15_v9_prompt.txt"
STAGE4_PROMPT_PATH = ROOT / "prompts" / "evidence15_v9_stage4_prompt.txt"
FREEZE_PATH = ROOT / "prompts" / "evidence15_v9_stage3_freeze.txt"
RUN_SCRIPT_PATH = ROOT / "tools" / "run_chemistry_evidence15_v9_stage4_teacher0724_591.sh"


def load_stage4_prompt() -> str:
    namespace: dict[str, object] = {}
    exec(STAGE4_PROMPT_PATH.read_text(encoding="utf-8"), namespace)
    return str(namespace["DIFFICULTY_RATING_PROMPT_PREFIX"])


class Evidence15V9Stage4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prompt = load_stage4_prompt()

    def test_stage3_prompt_is_frozen_by_hash(self) -> None:
        freeze = json.loads(FREEZE_PATH.read_text(encoding="utf-8"))
        actual = hashlib.sha256(STAGE3_PROMPT_PATH.read_bytes()).hexdigest()
        self.assertEqual(freeze["sha256"], actual)
        self.assertEqual(freeze["concurrency"], 30)
        self.assertIn("teacher-cleaned CSV", freeze["truth_source"])

    def test_stage4_has_two_independent_final_level_routes(self) -> None:
        self.assertIn("压轴题有两条互不替代的成立通道", self.prompt)
        self.assertIn("A. 跨阶段联合模型", self.prompt)
        self.assertIn("B. 高密度完整求解", self.prompt)
        self.assertIn("不得把A通道误写成唯一必要条件", self.prompt)

    def test_dense_route_counts_real_decisions_not_surface_length(self) -> None:
        self.assertIn("6个不可省略且不重复的有效化学决策", self.prompt)
        self.assertIn("至少两类实质处理 + 至少4个决策连续使用", self.prompt)
        self.assertIn("纯识记、抄写方程式、重复代入、纯算术、多空和题面篇幅不得计入", self.prompt)
        self.assertIn("一个高阶卡点后接若干机械步骤，也不能进入B通道", self.prompt)

    def test_old_single_mandatory_route_is_removed(self) -> None:
        self.assertNotIn("压轴题要求形成跨阶段联合模型", self.prompt)
        self.assertNotIn("必须写清至少两个高阶处理阶段，并说明", self.prompt)
        self.assertNotIn("不能指出两个高阶处理阶段以及前序结果", self.prompt)

    def test_controlled_variables_are_preserved(self) -> None:
        script = RUN_SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("evidence15_v9_stage4_prompt.txt", script)
        self.assertIn("--concurrency 30", script)
        self.assertIn("evidence15_boundary_rules_v9_stage1", script)
        self.assertIn('--labels "${LABELS}"', script)
        self.assertIn("顶层 difficulty 是旧输入错误标签", script)


if __name__ == "__main__":
    unittest.main()
