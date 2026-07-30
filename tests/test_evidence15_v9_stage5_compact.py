from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

from tests.test_evidence15_v9_schema_retry import load_runner


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chemistry_evidence15_v9_schema import (  # noqa: E402
    CORE_FEATURE_FIELDS,
    CORE_FEATURE_VALUES,
)


STAGE5_PATH = ROOT / "prompts" / "evidence15_v9_stage5_prompt.txt"
COMPACT_PATH = ROOT / "prompts" / "evidence15_v9_stage5_compact_prompt.txt"
RUN_SCRIPT_PATH = (
    ROOT / "tools" / "run_chemistry_evidence15_v9_stage5_compact_teacher0724_591.sh"
)


def load_prompt(path: Path) -> tuple[str, str]:
    namespace: dict[str, object] = {}
    exec(path.read_text(encoding="utf-8"), namespace)
    return (
        str(namespace["DIFFICULTY_RATING_PROMPT_PREFIX"]),
        str(namespace["DIFFICULTY_RATING_PROMPT_SUFFIX"]),
    )


class Evidence15V9Stage5CompactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stage5, cls.stage5_suffix = load_prompt(STAGE5_PATH)
        cls.compact, cls.compact_suffix = load_prompt(COMPACT_PATH)

    def test_compact_prompt_meets_character_budget(self) -> None:
        self.assertLessEqual(len(self.compact), 22_000)
        self.assertLess(len(self.compact), len(self.stage5) * 0.60)
        self.assertEqual(self.compact.count("你是一位资深的初中化学教研专家"), 1)

    def test_stage5_high_boundary_logic_is_preserved(self) -> None:
        required = (
            "用不可分离强关系统一终审",
            "模型选择",
            "共同闭合",
            "状态更新",
            "已知结果替代测试",
            "普通结果传递或数值代入",
            "不能直接降为3档",
            "必须独立检查是否存在4档的决定性卡点或综合主线",
        )
        for text in required:
            self.assertIn(text, self.stage5)
            self.assertIn(text, self.compact)

    def test_all_feature_fields_and_levels_are_preserved(self) -> None:
        self.assertEqual(len(CORE_FEATURE_FIELDS), 15)
        for index, field in enumerate(CORE_FEATURE_FIELDS, 1):
            self.assertIn(f"### {index}. {field}", self.compact)
        for level in ("送分题", "基础题", "中等题", "拔高题", "压轴题"):
            self.assertIn(level, self.compact)

    def test_fixed_output_enums_still_exactly_match_schema(self) -> None:
        template = self.compact.split("## 九、固定生产JSON", 1)[1]
        for field, allowed in CORE_FEATURE_VALUES.items():
            match = re.search(rf'"{re.escape(field)}"\s*:\s*"([^"]+)"', template)
            self.assertIsNotNone(match, field)
            self.assertEqual(tuple(match.group(1).split("/")), allowed, field)

    def test_each_level_keeps_its_operational_contract(self) -> None:
        self.assertEqual(self.compact.count("### 核心判定依据"), 5)
        self.assertEqual(self.compact.count("### 细粒度特征"), 5)
        self.assertEqual(self.compact.count("### 最低进入门槛"), 5)
        self.assertEqual(self.compact.count("### 与相邻档的否决条件"), 5)

    def test_compaction_removes_repeated_long_form_sections(self) -> None:
        self.assertNotIn("#### 教师示例：", self.compact)
        self.assertNotIn("## 六、题型结构复核", self.compact)
        self.assertNotIn("### 常见误判警示", self.compact)
        self.assertNotIn("合法JSON示例：", self.compact)
        self.assertIn("## 五、相邻档位最小校准锚点", self.compact)
        self.assertIn("下面的固定生产JSON只表示字段结构和允许值", self.compact)

    def test_forbidden_stage4_counting_route_remains_absent(self) -> None:
        for text in (
            "B. 高密度完整求解",
            "6个不可省略",
            "6个不重复",
            "至少两类实质处理 + 至少4个",
            "两个独立部分各自达到4档",
        ):
            self.assertNotIn(text, self.compact)

    def test_production_loader_accepts_compact_prompt(self) -> None:
        runner = load_runner()
        runner.load_prompt_config(str(COMPACT_PATH))
        self.assertEqual(runner.DIFFICULTY_RATING_PROMPT_PREFIX, self.compact)
        self.assertTrue(runner.DIFFICULTY_RATING_PROMPT_SUFFIX.strip())
        self.assertEqual(self.compact_suffix, self.stage5_suffix)

    def test_runner_keeps_controlled_variables(self) -> None:
        script = RUN_SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("evidence15_v9_stage5_compact_prompt.txt", script)
        self.assertIn("evidence15_boundary_rules_v9_stage5_audit45", script)
        self.assertIn("--concurrency 30", script)
        self.assertIn('--labels "${LABELS}"', script)
        self.assertIn("顶层 difficulty 是旧输入错误标签", script)
        self.assertIn('if [[ ! -s "${RESULT}" ]]', script)


if __name__ == "__main__":
    unittest.main()
