from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from tests.test_evidence15_v9_schema_retry import load_runner, valid_result


ROOT = Path(__file__).resolve().parents[1]
STAGE3_PROMPT_PATH = ROOT / "prompts" / "evidence15_v9_prompt.txt"
STAGE5_PROMPT_PATH = ROOT / "prompts" / "evidence15_v9_stage5_prompt.txt"
FREEZE_PATH = ROOT / "prompts" / "evidence15_v9_stage3_freeze.txt"
RUN_SCRIPT_PATH = ROOT / "tools" / "run_chemistry_evidence15_v9_stage5_teacher0724_591.sh"


def load_stage5_prompt() -> str:
    namespace: dict[str, object] = {}
    exec(STAGE5_PROMPT_PATH.read_text(encoding="utf-8"), namespace)
    return str(namespace["DIFFICULTY_RATING_PROMPT_PREFIX"])


class Evidence15V9Stage5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prompt = load_stage5_prompt()

    def test_stage5_is_built_directly_from_frozen_stage3(self) -> None:
        freeze = json.loads(FREEZE_PATH.read_text(encoding="utf-8"))
        canonical = (
            STAGE3_PROMPT_PATH.read_text(encoding="utf-8")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
        self.assertEqual(
            freeze["sha256"],
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
        source = STAGE5_PROMPT_PATH.read_text(encoding="utf-8")
        self.assertIn('Path.cwd() / "prompts" / "evidence15_v9_prompt.txt"', source)
        self.assertNotIn("evidence15_v9_stage4_prompt.txt", source)

    def test_production_loader_accepts_stage5_prompt(self) -> None:
        runner = load_runner()
        runner.load_prompt_config(str(STAGE5_PROMPT_PATH))
        self.assertIn("用不可分离强关系统一终审", runner.DIFFICULTY_RATING_PROMPT_PREFIX)
        self.assertTrue(runner.DIFFICULTY_RATING_PROMPT_SUFFIX.strip())

    def test_final_boundary_has_one_structural_standard(self) -> None:
        self.assertIn("用不可分离强关系统一终审", self.prompt)
        self.assertIn("模型选择", self.prompt)
        self.assertIn("共同闭合", self.prompt)
        self.assertIn("状态更新", self.prompt)
        self.assertIn("已知结果替代测试", self.prompt)
        self.assertIn("普通结果传递或数值代入", self.prompt)

    def test_counting_route_and_parallel_hard_modules_are_absent(self) -> None:
        for text in (
            "A. 跨阶段联合模型",
            "B. 高密度完整求解",
            "6个不可省略",
            "6个不重复",
            "至少两类实质处理 + 至少4个",
            "两个独立部分各自达到4档",
        ):
            self.assertNotIn(text, self.prompt)

    def test_failed_final_test_does_not_bypass_hard_level(self) -> None:
        self.assertIn("不能直接降为3档", self.prompt)
        self.assertIn("必须独立检查是否存在4档的决定性卡点或综合主线", self.prompt)
        self.assertIn("5档不成立后仍须独立复核4档", self.prompt)

    def test_final_output_requires_reproducible_audit(self) -> None:
        self.assertIn("高阶任务H1：", self.prompt)
        self.assertIn("高阶任务H2：", self.prompt)
        self.assertIn("关系类型：模型选择/共同闭合/状态更新", self.prompt)
        self.assertIn("普通数值传递", self.prompt)

    def test_stage5_runner_controls_truth_profile_and_concurrency(self) -> None:
        script = RUN_SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("evidence15_v9_stage5_prompt.txt", script)
        self.assertIn("evidence15_boundary_rules_v9_stage5_audit45", script)
        self.assertIn("--concurrency 30", script)
        self.assertIn('--labels "${LABELS}"', script)
        self.assertIn("顶层 difficulty 是旧输入错误标签", script)
        self.assertIn("postprocess_audit_actions", script)
        self.assertIn('if [[ ! -s "${RESULT}" ]]', script)
        self.assertIn("停止评测和打包", script)

    def test_automatic_hard_to_final_is_audit_only(self) -> None:
        runner = load_runner()
        source = valid_result()
        source["difficulty_level"] = "拔高题"
        source["coarse_difficulty"] = "拔高/压轴区间（4-5档）"
        source["features"].update({
            "entry_operation": "多节点连续决策",
            "task_rule_breadth": "存在结果或任务链依赖",
            "visual_information_role": "决定模型、阶段或任务链",
            "reasoning_depth": "6层及以上",
            "reasoning_direction": "分类讨论或综合推导",
            "knowledge_relation": "多模块深度融合",
            "representation_conversion": "宏观-微观-符号-定量多重转换",
            "reaction_relation": "多反应连续转化",
            "constraint_complexity": "多层嵌套约束",
            "evidence_relation": "证据冲突、筛选或多层排除",
            "experiment_requirement": "多阶段探究与定量误差",
            "graph_table_requirement": "多图表耦合建模",
            "calculation_model": "多重守恒、差量、联立或分类",
            "unfamiliar_information_transfer": "完全陌生模型现场建立",
            "subquestion_dependency": "多问存在结果或任务链依赖",
        })
        old_profile = runner.CORE12_POSTPROCESS_PROFILE
        runner.CORE12_POSTPROCESS_PROFILE = "evidence15_boundary_rules_v9_stage5_audit45"
        try:
            result = runner.postprocess_chemistry_difficulty(
                copy.deepcopy(source),
                {
                    "stem": (
                        "任务一设计实验并排除干扰；任务二根据所得物质继续过量反应；"
                        "任务三根据曲线计算质量分数。"
                    ),
                },
            )
        finally:
            runner.CORE12_POSTPROCESS_PROFILE = old_profile

        self.assertEqual(result["difficulty_level"], "拔高题")
        self.assertFalse(result["automatic_level_change_applied"])
        self.assertEqual(result["postprocess_actions"], [])
        self.assertEqual(result["postprocess_trace"], [])
        self.assertEqual(len(result["postprocess_audit_actions"]), 1)
        audit = result["postprocess_audit_actions"][0]
        self.assertEqual(audit["rule"], "dataaware_hard_to_final")
        self.assertEqual(audit["from"], "拔高题")
        self.assertEqual(audit["to"], "压轴题")
        self.assertEqual(audit["mode"], "audit_only")
        self.assertFalse(audit["applied"])

    def test_stage5_keeps_non_45_boundary_actions_enabled(self) -> None:
        runner = load_runner()
        source = valid_result()
        source["difficulty_level"] = "基础题"
        source["coarse_difficulty"] = "送分/基础区间（1-2档）"
        source["features"].update({
            "entry_operation": "一次透明映射",
            "task_rule_breadth": "单一规则或同类检索束",
            "visual_information_role": "直接识图作答",
            "reasoning_depth": "1层",
            "reasoning_direction": "正向推导",
            "knowledge_relation": "单一知识点",
            "representation_conversion": "一次表征转换",
            "reaction_relation": "无反应关系",
            "constraint_complexity": "无约束",
            "evidence_relation": "单一证据直接对应",
            "experiment_requirement": "无",
            "graph_table_requirement": "直接读数",
            "calculation_model": "无",
            "unfamiliar_information_transfer": "课内直接原型",
            "subquestion_dependency": "多问相互独立",
        })
        old_profile = runner.CORE12_POSTPROCESS_PROFILE
        runner.CORE12_POSTPROCESS_PROFILE = "evidence15_boundary_rules_v9_stage5_audit45"
        try:
            result = runner.postprocess_chemistry_difficulty(
                copy.deepcopy(source),
                {"stem": "根据图中六个安全标志分别写出名称。"},
            )
        finally:
            runner.CORE12_POSTPROCESS_PROFILE = old_profile

        self.assertEqual(result["difficulty_level"], "送分题")
        self.assertTrue(result["automatic_level_change_applied"])
        self.assertEqual(
            result["postprocess_actions"][0]["rule"],
            "evidence15_basic_to_easy",
        )
        self.assertEqual(result["postprocess_audit_actions"], [])


if __name__ == "__main__":
    unittest.main()
