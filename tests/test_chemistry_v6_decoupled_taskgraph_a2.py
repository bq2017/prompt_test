from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT / "tools"))

from chemistry_two_pass_taskgraph_schema import (  # noqa: E402
    TwoPassSchemaError,
    UnreconstructableInputError,
    validate_rating,
    validate_reconstruction,
)
from evaluate_chemistry_difficulty import extract_prediction  # noqa: E402


TASK_PROMPT = ROOT / "prompts" / "0730初中化学任务重建提示词_v6_decoupled_taskgraph_a2.txt"
RATING_PROMPT = ROOT / "prompts" / "0730初中化学难度打标提示词_v6_decoupled_taskgraph_a2.txt"
RUNNER = ROOT / "src" / "chemistry_difficulty_rating_0730_v6_decoupled_taskgraph_a2_with_cache.py"
V6_PROMPT = ROOT / "prompts" / "0730初中化学难度打标提示词_v5_2_evidence15_v6.txt"
A1_PROMPT = ROOT / "prompts" / "0730初中化学难度打标提示词_v6_decoupled_taskgraph_a1.txt"


def normalized_text_sha256(path: Path) -> str:
    """Hash prompt content independent of Git checkout line endings."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def reconstruction() -> dict:
    return {
        "input_assessment": {
            "can_reconstruct": True,
            "stem_readability": "完整",
            "solution_readability": "未提供但题面可独立重建",
            "missing_information": [],
            "conflict_description": "无",
        },
        "reconstructed_question": {
            "complete_task_summary": "根据同一流程分别判断滤渣和滤液，再用滤液组成写方程式。",
            "subquestions": ["第（1）问判断滤渣", "第（2）问判断滤液并写方程式"],
            "source_resolution": "题干文字与流程图互相一致。",
        },
        "rated_target": {
            "scope": "第（2）问",
            "task": "根据滤液组成选择并写出反应方程式",
            "included_prerequisites": ["判断滤液组成"],
            "selection_reason": "包含结果复用，要求高于独立滤渣判断。",
        },
        "task_graph": {
            "nodes": [
                {
                    "id": "N1", "subquestion": "第（1）问", "primary_kind": "reaction",
                    "tags": ["constraint"], "counts_as_decision": True,
                    "action": "依据流程判断滤渣组成", "output": "滤渣组成",
                    "source_evidence": "流程中的反应物和过滤节点",
                },
                {
                    "id": "N2", "subquestion": "第（2）问", "primary_kind": "reaction",
                    "tags": ["constraint"], "counts_as_decision": True,
                    "action": "依据物质去向判断滤液组成", "output": "滤液组成",
                    "source_evidence": "流程中加入试剂及过滤后的去向",
                },
                {
                    "id": "N3", "subquestion": "第（2）问", "primary_kind": "reaction",
                    "tags": ["rule_application"], "counts_as_decision": True,
                    "action": "由滤液组成选择并写出反应", "output": "有效反应方程式",
                    "source_evidence": "第（2）问要求和N2输出",
                },
            ],
            "edges": [
                {"from": "N2", "to": "N3", "type": "result_reuse", "basis": "N3直接使用N2得到的滤液组成"}
            ],
            "shared_models": [
                {"id": "M1", "description": "流程中的物质去向模型", "node_ids": ["N1", "N2"], "basis": "两节点读取同一流程但互不复用答案"}
            ],
        },
        "audit_attributes": {
            "question_relation": "多问共享模型但无结果依赖",
            "visual_role": "提供局部关系",
            "reaction_structure": "简单连续反应",
            "experiment_structure": "无实验任务",
            "graph_table_structure": "无图表任务",
            "calculation_structure": "无定量任务",
            "information_transfer": "课内直接原型",
        },
    }


def rating() -> dict:
    return {
        "difficulty_level": "中等题",
        "rating_dimensions": {
            "dependency_demand": "常规连续模型",
            "model_demand": "完整常规模型",
            "evidence_constraint_demand": "常规联合证据或约束",
            "quantitative_demand": "无定量",
            "transfer_demand": "课内原型",
        },
        "boundary_review": {
            "why_not_lower": "需先判断滤液组成，再选择有效反应。",
            "why_not_higher": "没有改变路径的先后、过量或竞争判断。",
        },
        "decisive_reason": "存在一条常规结果复用链，但模型唯一。",
        "task_graph_review": {"status": "一致", "explanation": "节点和真实依赖足以支持定档。"},
        "special_anchor": {"type": "无", "evidence": "不涉及固定硫酸根整体拆分。"},
    }


class PromptIsolationTests(unittest.TestCase):
    def test_task_prompt_contains_no_level_names(self) -> None:
        text = TASK_PROMPT.read_text(encoding="utf-8")
        for level in ("送分题", "基础题", "中等题", "拔高题", "压轴题"):
            self.assertNotIn(level, text)
        self.assertIn("shared_models", text)
        self.assertIn("counts_as_decision", text)
        self.assertNotIn("model_dependency", text)

    def test_rating_prompt_restores_domain_protocols(self) -> None:
        text = RATING_PROMPT.read_text(encoding="utf-8")
        for heading in ("反应与组分关系", "实验与证据", "图像与表格", "定量模型", "陌生信息迁移与表征转换"):
            self.assertIn(heading, text)
        self.assertIn("固定基团教师锚点", text)
        self.assertGreaterEqual(text.count("【例题"), 10)

    def test_runner_uses_separate_prefix_caches(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn('"task_reconstruction"', text)
        self.assertIn('"difficulty_rating"', text)
        self.assertIn("separate_calls_separate_prefix_caches", text)
        self.assertIn("automatic_level_change_applied", text)

    def test_control_prompts_are_unchanged(self) -> None:
        self.assertEqual(
            normalized_text_sha256(V6_PROMPT),
            "AE50AA0FEAFD170651975728FB54C086A11C782F8F4B1B42B078FBF97976B871",
        )
        self.assertEqual(
            normalized_text_sha256(A1_PROMPT),
            "94E8F346A6F9C49664811FDBE8919594FF345DB02F6C015103FADEBC787943B8",
        )


class SchemaTests(unittest.TestCase):
    def test_shared_model_does_not_create_directed_path(self) -> None:
        value = validate_reconstruction(reconstruction())
        metrics = value["derived_graph_metrics"]
        self.assertEqual(metrics["shared_model_count"], 1)
        self.assertEqual(metrics["dependency_edge_count"], 1)
        self.assertEqual(metrics["longest_decision_path_nodes"], 2)

    def test_program_derives_neighbors_and_never_changes_level(self) -> None:
        reconstructed = validate_reconstruction(reconstruction())
        value = validate_rating(rating(), reconstructed)
        self.assertEqual(value["difficulty_level"], "中等题")
        self.assertEqual(value["boundary_review"]["lower_level"], "基础题")
        self.assertEqual(value["boundary_review"]["upper_level"], "拔高题")
        self.assertFalse(value["automatic_level_change_applied"])
        item = {"difficulty_rating": value}
        self.assertEqual(extract_prediction(item, "pre-postprocess"), ("中等题", 3))
        self.assertEqual(extract_prediction(item, "final"), ("中等题", 3))

    def test_model_dependency_edge_is_rejected(self) -> None:
        raw = reconstruction()
        raw["task_graph"]["edges"][0]["type"] = "model_dependency"
        with self.assertRaisesRegex(TwoPassSchemaError, "type非法"):
            validate_reconstruction(raw)

    def test_cycle_is_rejected(self) -> None:
        raw = reconstruction()
        raw["task_graph"]["edges"].append(
            {"from": "N3", "to": "N2", "type": "condition_dependency", "basis": "人为循环"}
        )
        with self.assertRaisesRegex(TwoPassSchemaError, "循环"):
            validate_reconstruction(raw)

    def test_unreconstructable_contract(self) -> None:
        raw = reconstruction()
        raw["input_assessment"] = {
            "can_reconstruct": False,
            "stem_readability": "关键缺失无法重建",
            "solution_readability": "关键缺失但题面仍可重建",
            "missing_information": ["曲线纵轴不可读"],
            "conflict_description": "无",
        }
        raw["reconstructed_question"] = None
        raw["rated_target"] = None
        raw["task_graph"] = {"nodes": [], "edges": [], "shared_models": []}
        raw["audit_attributes"] = None
        with self.assertRaises(UnreconstructableInputError):
            validate_reconstruction(raw)

    def test_extra_evidence15_fields_are_rejected(self) -> None:
        raw = reconstruction()
        raw["features"] = {"reasoning_depth": "2-3层"}
        with self.assertRaisesRegex(TwoPassSchemaError, "extra"):
            validate_reconstruction(raw)


if __name__ == "__main__":
    unittest.main()
