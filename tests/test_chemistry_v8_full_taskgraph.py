from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT / "tools"))

from chemistry_single_pass_compact_schema import (  # noqa: E402
    CompactSchemaError,
    validate_and_prepare_compact_result,
)
from chemistry_v8_full_taskgraph_schema import (  # noqa: E402
    UnrateableInputError,
    V8SchemaError,
    apply_v8_observe_only_postprocess,
    validate_and_prepare_v8_result,
)
from evaluate_chemistry_difficulty import extract_prediction  # noqa: E402


PROMPT = ROOT / "prompts" / "0730初中化学难度打标提示词_v8_full_taskgraph.txt"
RUNNER = ROOT / "src" / "chemistry_difficulty_rating_0730_v8_full_taskgraph_with_cache.py"
V6_PROMPT = ROOT / "prompts" / "0730初中化学难度打标提示词_v5_2_evidence15_v6.txt"
A3_PROMPT = ROOT / "prompts" / "0730初中化学难度打标提示词_v6_single_pass_compact_a3.txt"


def normalized_text_sha256(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def load_prompt() -> tuple[str, str]:
    namespace = {"__file__": str(PROMPT)}
    exec(compile(PROMPT.read_text(encoding="utf-8"), str(PROMPT), "exec"), namespace)
    return namespace["DIFFICULTY_RATING_PROMPT_PREFIX"], namespace["DIFFICULTY_RATING_PROMPT_SUFFIX"]


def v8_rating() -> dict:
    return {
        "input_assessment": {
            "can_rate": True,
            "stem_readability": "完整",
            "solution_readability": "未提供但题面可独立定档",
            "missing_information": [],
            "conflict_description": "无",
        },
        "rated_target": {
            "scope": "第（2）问",
            "task": "根据滤液组成选择并写出有效反应方程式",
            "included_prerequisites": ["判断滤液中的溶质组成"],
        },
        "primary_dimensions": {
            "dependency_structure": {"level": "完整常规链", "basis": "先判断组分，再选择反应。"},
            "decisive_transformation": {"level": "仅常规转换", "basis": "组分限制反应，但路径属于课内常规。"},
            "model_requirement": {"level": "完整常规模型", "basis": "需要维持同一流程中的物质去向模型。"},
            "evidence_constraints": {"level": "联合证据或关联约束", "basis": "加入物质和过滤去向共同确定滤液。"},
            "coupling_structure": {"level": "存在结果复用", "basis": "滤液结论进入方程式选择。"},
        },
        "task_graph": {
            "nodes": [
                {
                    "id": "N1",
                    "subquestion": "第（1）问",
                    "kind": "reaction",
                    "input": "流程中的加入物与过滤关系",
                    "action": "判断滤液中的实际溶质组成",
                    "output": "滤液溶质集合",
                    "source_evidence": "题干流程箭头与过滤节点",
                },
                {
                    "id": "N2",
                    "subquestion": "第（2）问",
                    "kind": "reaction",
                    "input": "N1得到的滤液溶质集合",
                    "action": "选择可发生反应并写出方程式",
                    "output": "有效反应方程式",
                    "source_evidence": "第（2）问设问与N1结果",
                },
            ],
            "edges": [
                {
                    "from": "N1",
                    "to": "N2",
                    "type": "result_reuse",
                    "transferred_object": "滤液溶质集合",
                    "basis": "实际溶质决定后续可选反应物。",
                }
            ],
            "shared_models": [
                {
                    "id": "M1",
                    "description": "同一流程中的物质去向模型",
                    "node_ids": ["N1", "N2"],
                    "basis": "两个任务共同读取同一流程，但共享模型本身不另建有向边。",
                }
            ],
        },
        "audit_attributes": {
            "question_relation": "多问存在结果或任务链依赖",
            "visual_role": "提供局部关系",
            "reaction_structure": "连续反应链",
            "experiment_structure": "无实验任务",
            "graph_table_structure": "无图表任务",
            "calculation_structure": "无计算",
            "information_transfer": "课内直接原型",
        },
        "boundary_review": {
            "why_not_lower": "需要先确定滤液组成，再完成后续反应选择。",
            "why_not_higher": "不存在过量、竞争解释或模型切换等决定性高阶卡点。",
        },
        "decisive_reason": "最高难任务形成一条常规结果复用链，但没有改变路径的高阶卡点。",
        "difficulty_level": "中等题",
    }


class PromptAndCompatibilityTests(unittest.TestCase):
    def test_prompt_is_complete_and_executable(self) -> None:
        prefix, suffix = load_prompt()
        self.assertEqual(
            normalized_text_sha256(PROMPT),
            "660298AE99A00B9D2CFE70EA648500D11927B5C88325A9E3AB5131237B5286FE",
        )
        self.assertGreater(len(prefix), 17000)
        self.assertGreaterEqual(prefix.count("#### 示例"), 11)
        self.assertIn("## 六、最小充分任务图规范", prefix)
        self.assertIn("transferred_object", prefix)
        self.assertIn("shared_models", prefix)
        self.assertIn("最终只输出一个合法JSON对象", suffix)

    def test_v8_requires_adapter_instead_of_a3_schema(self) -> None:
        with self.assertRaises(CompactSchemaError):
            validate_and_prepare_compact_result(v8_rating())
        value = validate_and_prepare_v8_result(v8_rating())
        self.assertTrue(value["schema_validation_passed"])

    def test_runner_is_single_call_and_observe_only(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn('STAGE = "v8_single_call_full_taskgraph"', source)
        self.assertIn("apply_v8_observe_only_postprocess", source)
        self.assertIn("automatic_level_change_applied", source)
        self.assertNotIn("chemistry_core12_schema", source)

    def test_control_prompts_are_unchanged(self) -> None:
        self.assertEqual(
            normalized_text_sha256(V6_PROMPT),
            "AE50AA0FEAFD170651975728FB54C086A11C782F8F4B1B42B078FBF97976B871",
        )
        self.assertEqual(
            normalized_text_sha256(A3_PROMPT),
            "63F0064D260DCFB57581830A1CA320E5478934F857778D6AE4B9494745A35A86",
        )


class SchemaAndPostprocessTests(unittest.TestCase):
    def test_graph_metrics_keep_shared_model_out_of_path(self) -> None:
        value = validate_and_prepare_v8_result(v8_rating())
        metrics = value["derived_taskgraph_metrics"]
        self.assertEqual(metrics["node_count"], 2)
        self.assertEqual(metrics["dependency_edge_count"], 1)
        self.assertEqual(metrics["shared_model_count"], 1)
        self.assertEqual(metrics["longest_dependency_path_edges"], 1)

    def test_cycle_is_rejected(self) -> None:
        raw = v8_rating()
        raw["task_graph"]["edges"].append({
            "from": "N2",
            "to": "N1",
            "type": "condition_dependency",
            "transferred_object": "人为循环条件",
            "basis": "测试循环",
        })
        with self.assertRaisesRegex(V8SchemaError, "有向无环图"):
            validate_and_prepare_v8_result(raw)

    def test_unrateable_contract(self) -> None:
        raw = v8_rating()
        raw["input_assessment"] = {
            "can_rate": False,
            "stem_readability": "关键缺失无法定档",
            "solution_readability": "未提供但题面可独立定档",
            "missing_information": ["曲线纵轴不可读"],
            "conflict_description": "无",
        }
        raw["rated_target"] = None
        raw["primary_dimensions"] = None
        raw["task_graph"] = {"nodes": [], "edges": [], "shared_models": []}
        raw["audit_attributes"] = None
        raw["boundary_review"] = None
        raw["difficulty_level"] = None
        raw["decisive_reason"] = "曲线纵轴缺失会改变阶段判断，无法可靠定档。"
        with self.assertRaises(UnrateableInputError):
            validate_and_prepare_v8_result(raw)

    def test_postprocess_flags_but_never_changes_level(self) -> None:
        raw = v8_rating()
        raw["difficulty_level"] = "拔高题"
        raw["task_graph"]["edges"] = []
        value = apply_v8_observe_only_postprocess(validate_and_prepare_v8_result(raw))
        codes = {flag["code"] for flag in value["v8_audit_flags"]}
        self.assertIn("hard_without_decisive_edge_tension", codes)
        self.assertEqual(value["postprocess_original_level"], "拔高题")
        self.assertEqual(value["postprocess_final_level"], "拔高题")
        self.assertEqual(value["difficulty_level"], "拔高题")
        self.assertFalse(value["automatic_level_change_applied"])
        item = {"difficulty_rating": value}
        self.assertEqual(extract_prediction(item, "pre-postprocess"), ("拔高题", 4))
        self.assertEqual(extract_prediction(item, "final"), ("拔高题", 4))


if __name__ == "__main__":
    unittest.main()
