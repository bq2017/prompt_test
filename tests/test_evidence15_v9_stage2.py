from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import chemistry_evidence15_v9_schema as stage1_schema  # noqa: E402


PROMPT_PATH = ROOT / "prompts" / "evidence15_v9_prompt.txt"
RUNNER_PATH = ROOT / "src" / "chemistry_difficulty_rating_evidence15_v9_with_cache.py"
RUN_SCRIPT_PATH = ROOT / "tools" / "run_chemistry_evidence15_v9_stage2_teacher0724_591.sh"


def load_prompt() -> str:
    namespace: dict[str, object] = {}
    exec(PROMPT_PATH.read_text(encoding="utf-8"), namespace)
    return str(namespace["DIFFICULTY_RATING_PROMPT_PREFIX"])


def valid_result() -> dict:
    return {
        "features": {
            "entry_operation": "形成中间结论后应用",
            "task_rule_breadth": "单一规则或同类检索束",
            "visual_information_role": "无实质视觉信息",
            "reasoning_depth": "2-3层",
            "reasoning_direction": "正向推导",
            "knowledge_relation": "同模块深度关联",
            "representation_conversion": "一次表征转换",
            "reaction_relation": "单一直接反应",
            "constraint_complexity": "单一约束",
            "evidence_relation": "多条清晰证据联合",
            "experiment_requirement": "无",
            "graph_table_requirement": "无",
            "calculation_model": "单一方程式或关系式",
            "unfamiliar_information_transfer": "课内直接原型",
            "subquestion_dependency": "无多问",
        },
        "coarse_difficulty": "基础/中等区间（2-3档）",
        "reasoning": {
            "core_basis": "同一常规模型中完成证据联合和关系式计算。",
            "hard_point": "常规关系建立。",
            "why_not_lower": "超过一次应用。",
            "why_not_higher": "没有高阶卡点或高密度闭环。",
        },
        "difficulty_level": "中等题",
    }


class Evidence15V9Stage2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prompt = load_prompt()

    def test_seven_steps_allow_fact_audit_before_final_review(self) -> None:
        self.assertIn("## 一、统一的七步处理流程", self.prompt)
        coarse = self.prompt.index("### 第四步：五维初判并选择相邻粗区间")
        features = self.prompt.index("### 第五步：填写15项难度审计特征")
        final_review = self.prompt.index("### 第六步：回到真实任务完成相邻边界终审")
        self.assertLess(coarse, features)
        self.assertLess(features, final_review)

    def test_obsolete_structure_terms_are_absent(self) -> None:
        for text in ("任务图", "任务边", "任务台账", "三点五", "最终档位确定后", "六步流程"):
            self.assertNotIn(text, self.prompt)

    def test_low_boundary_requires_real_conversion_not_breadth(self) -> None:
        self.assertIn("多个彼此独立的直接检索点", self.prompt)
        self.assertIn("B只用于描述广度，不能替代这次真实转换", self.prompt)
        self.assertIn("若最高难任务仍为D=0", self.prompt)

    def test_middle_boundary_has_dependency_and_complete_closure_paths(self) -> None:
        self.assertIn("前一步产生的新结论被后一步使用", self.prompt)
        self.assertIn("共同完成一个不可缺少的常规任务闭环", self.prompt)
        self.assertIn("不要求答案被下一小问直接复用", self.prompt)

    def test_hard_and_final_boundaries_have_two_structural_paths(self) -> None:
        self.assertIn("拔高题有两条成立路径", self.prompt)
        self.assertIn("约5—6个非重复有效决策", self.prompt)
        self.assertIn("也可以由完整的模型依赖成立", self.prompt)
        self.assertIn("可以发生在同一设问内部", self.prompt)

    def test_schema_contract_keeps_fields_and_records_stage2_metadata(self) -> None:
        source = valid_result()
        after = stage1_schema.validate_and_prepare_result(source, {})
        self.assertEqual(source["difficulty_level"], after["difficulty_level"])
        self.assertEqual(source["features"], after["features"])
        self.assertEqual(after["postprocess_profile"], "evidence15_v9_schema_only")
        self.assertEqual(after["feature_schema_version"], "chemistry_evidence15_v9_stage2")

    def test_stage1_postprocess_is_intentionally_unchanged(self) -> None:
        source = valid_result()
        source["features"].update({
            "visual_information_role": "决定模型、阶段或任务链",
            "representation_conversion": "两类表征连续转换",
            "constraint_complexity": "多个相互关联约束",
            "graph_table_requirement": "拐点、平台或分段反推",
            "experiment_requirement": "控制变量、现象解释或数据归纳",
        })
        data = {"stem": "根据曲线平台和拐点选择数据并计算质量分数。"}
        after = stage1_schema.apply_data_aware_boundary_rules(
            stage1_schema.validate_and_prepare_result(source, data), data
        )
        self.assertEqual(after["difficulty_level"], "拔高题")
        self.assertEqual(after["postprocess_actions"][0]["rule"], "dataaware_medium_to_hard")

    def test_runner_uses_stage2_prompt_and_keeps_stage1_profile(self) -> None:
        source = RUNNER_PATH.read_text(encoding="utf-8")
        script = RUN_SCRIPT_PATH.read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn('PROMPT_FILENAME = "evidence15_v9_prompt.txt"', source)
        self.assertIn('"evidence15_boundary_rules_v9_stage1"', source)
        self.assertIn('export CHEMISTRY_EVIDENCE15_V9_POSTPROCESS_PROFILE="evidence15_boundary_rules_v9_stage1"', script)
        self.assertIn("--concurrency 30", script)

    def test_prompt_python_and_json_example_are_parseable(self) -> None:
        self.assertEqual(len(stage1_schema.CORE_FEATURE_FIELDS), 15)
        marker = "合法JSON示例："
        start = self.prompt.index("{", self.prompt.index(marker))
        example, _ = json.JSONDecoder().raw_decode(self.prompt[start:])
        self.assertEqual(tuple(example["features"]), stage1_schema.CORE_FEATURE_FIELDS)


if __name__ == "__main__":
    unittest.main()
