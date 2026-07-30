from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / "prompts" / "evidence15_v9_prompt.txt"
RUNNER_PATH = ROOT / "src" / "chemistry_difficulty_rating_evidence15_v9_with_cache.py"
RUN_SCRIPT_PATH = ROOT / "tools" / "run_chemistry_evidence15_v9_stage23_teacher0724_591.sh"


def load_prompt() -> str:
    namespace: dict[str, object] = {}
    exec(PROMPT_PATH.read_text(encoding="utf-8"), namespace)
    return str(namespace["DIFFICULTY_RATING_PROMPT_PREFIX"])


class Evidence15V9Stage23Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prompt = load_prompt()

    def test_low_boundary_uses_depth_and_total_burden(self) -> None:
        self.assertIn("同时判断单项操作D与整题任务负担B", self.prompt)
        self.assertIn("单一固定对应，变化对象不改变核验规则", self.prompt)
        self.assertIn("多个不同判据的独立应用束", self.prompt)
        self.assertIn("至少两项不同的实际核验内容", self.prompt)

    def test_teacher_anchor_contrasts_are_present(self) -> None:
        self.assertIn("同一标准下的化学变化判断", self.prompt)
        self.assertIn("溶液说法的多判据独立核验", self.prompt)
        self.assertIn("化学方程式的不同错误类型核验", self.prompt)

    def test_stage23_keeps_controlled_variables(self) -> None:
        runner = RUNNER_PATH.read_text(encoding="utf-8")
        script = RUN_SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("Evidence-15 V9阶段3", runner)
        self.assertIn("evidence15_v9_stage2_3_591_", script)
        self.assertIn("--concurrency 30", script)
        self.assertIn("evidence15_boundary_rules_v9_stage1", script)
        self.assertIn('--labels "${LABELS}"', script)
        self.assertIn("顶层 difficulty 是旧输入错误标签", script)


if __name__ == "__main__":
    unittest.main()
