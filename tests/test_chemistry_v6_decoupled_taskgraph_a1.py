from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from chemistry_decoupled_taskgraph_schema import (  # noqa: E402
    DecoupledSchemaError,
    UnrateableInputError,
    apply_observe_only_audit,
    validate_and_prepare_result,
)


PROMPT = ROOT / "prompts" / "0730初中化学难度打标提示词_v6_decoupled_taskgraph_a1.txt"
V6_PROMPT = ROOT / "prompts" / "0730初中化学难度打标提示词_v5_2_evidence15_v6.txt"


def normalized_text_sha256(path: Path) -> str:
    """Hash prompt content independent of Git checkout line endings."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


RUNNER = ROOT / "src" / "chemistry_difficulty_rating_0730_v6_decoupled_taskgraph_a1_with_cache.py"


def valid_result() -> dict:
    return {
        "input_assessment": {
            "can_rate": True,
            "stem_readability": "完整",
            "solution_readability": "完整",
            "missing_information": [],
            "conflict_description": "无",
        },
        "primary_dimensions": {
            "dependency_structure": "先判断反应，再把反应结论用于质量关系。",
            "decisive_transformation": "没有会改变路径的反应先后或过量条件。",
            "model_requirement": "需要建立一套常规单反应化学计量模型。",
            "evidence_constraints": "题面给定质量只进入同一显性模型。",
            "coupling_structure": "两个任务存在结果复用，但没有跨模型耦合。",
        },
        "task_graph": {
            "nodes": [
                {
                    "id": "N1",
                    "subquestion": "整题",
                    "kind": "reaction",
                    "action": "判断反应并写出方程式",
                    "output": "得到配平后的反应计量关系",
                    "source_evidence": "题干给出反应物和生成物条件",
                },
                {
                    "id": "N2",
                    "subquestion": "整题",
                    "kind": "calculation",
                    "action": "建立质量比例并计算",
                    "output": "得到生成物质量",
                    "source_evidence": "题干给定反应物质量",
                },
            ],
            "edges": [
                {
                    "from": "N1",
                    "to": "N2",
                    "type": "result_reuse",
                    "basis": "N2复用N1得到的化学计量关系",
                }
            ],
        },
        "boundary_review": {
            "lower_level": "基础题",
            "why_not_lower": "需要自行建立完整化学计量模型，不是直接比例代入。",
            "upper_level": "拔高题",
            "why_not_higher": "单反应单路径，无过量、分类或竞争解释。",
        },
        "decisive_reason": "完整常规单反应计算模型，对应中等题。",
        "difficulty_level": "中等题",
    }


class PromptContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        namespace: dict[str, object] = {}
        exec(PROMPT.read_text(encoding="utf-8"), namespace)
        cls.prompt = str(namespace["DIFFICULTY_RATING_PROMPT_PREFIX"])

    def test_decision_is_isolated_from_task_graph_audit(self) -> None:
        self.assertIn("独立主定档—任务图审计", self.prompt)
        self.assertIn("必须先完成本章的定档，再建立输出任务图", self.prompt)
        self.assertIn("任务图用于核查和后续稳定性分析，不得反向机械覆盖", self.prompt)
        example = self.prompt.split("可可靠定档时使用以下结构：", 1)[1]
        self.assertLess(example.index('"difficulty_level"'), example.index('"task_graph"'))

    def test_old_coupled_contract_is_absent(self) -> None:
        self.assertNotIn("12个核心特征", self.prompt)
        self.assertNotIn("12项核心特征", self.prompt)
        self.assertNotIn('"coarse_difficulty":', self.prompt)
        self.assertNotIn('"features"', self.prompt)

    def test_task_graph_and_unrateable_contract_are_explicit(self) -> None:
        for text in (
            '"task_graph"',
            '"nodes"',
            '"edges"',
            '"can_rate"',
            '"missing_information"',
            "difficulty_level=null",
        ):
            self.assertIn(text, self.prompt)

    def test_teacher_examples_use_one_consistent_format(self) -> None:
        self.assertEqual(self.prompt.count("【教师档位】"), 10)
        self.assertEqual(self.prompt.count("【必要任务图】"), 10)
        self.assertEqual(self.prompt.count("【边界说明】"), 10)

    def test_v6_prompt_is_unchanged(self) -> None:
        digest = normalized_text_sha256(V6_PROMPT)
        self.assertEqual(
            digest,
            "AE50AA0FEAFD170651975728FB54C086A11C782F8F4B1B42B078FBF97976B871",
        )

    def test_runner_uses_new_prompt_and_full_text_grounding(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn("0730初中化学难度打标提示词_v6_decoupled_taskgraph_a1.txt", source)
        self.assertIn("chemistry_decoupled_taskgraph_schema", source)
        self.assertIn("【完整结构化文字参考】", source)
        self.assertIn('"question_input_mode": "image_text_grounded"', source)


class SchemaContractTests(unittest.TestCase):
    def test_valid_result_derives_graph_metrics_without_changing_level(self) -> None:
        prepared = validate_and_prepare_result(valid_result())
        self.assertEqual(prepared["difficulty_level"], "中等题")
        self.assertEqual(prepared["derived_features"]["node_count"], 2)
        self.assertEqual(prepared["derived_features"]["dependency_edge_count"], 1)
        self.assertEqual(prepared["derived_features"]["longest_dependency_path_edges"], 1)
        self.assertFalse(prepared["automatic_level_change_applied"])

    def test_observe_only_profile_never_changes_level(self) -> None:
        raw = valid_result()
        raw["difficulty_level"] = "送分题"
        raw["boundary_review"] = {
            "lower_level": "无（最低档）",
            "why_not_lower": "已经是最低档。",
            "upper_level": "基础题",
            "why_not_higher": "模型独立裁定仍认为是直接识别。",
        }
        prepared = validate_and_prepare_result(raw)
        audited = apply_observe_only_audit(prepared)
        self.assertEqual(audited["difficulty_level"], "送分题")
        self.assertFalse(audited["automatic_level_change_applied"])

    def test_cycle_is_rejected(self) -> None:
        raw = valid_result()
        raw["task_graph"]["edges"].append(
            {
                "from": "N2",
                "to": "N1",
                "type": "condition_dependency",
                "basis": "构造循环用于测试",
            }
        )
        with self.assertRaisesRegex(DecoupledSchemaError, "循环"):
            validate_and_prepare_result(raw)

    def test_wrong_neighbor_is_rejected(self) -> None:
        raw = valid_result()
        raw["boundary_review"]["lower_level"] = "送分题"
        with self.assertRaisesRegex(DecoupledSchemaError, "lower_level"):
            validate_and_prepare_result(raw)

    def test_unrateable_input_is_logged_as_failure_instead_of_guessed(self) -> None:
        raw = valid_result()
        raw["input_assessment"] = {
            "can_rate": False,
            "stem_readability": "关键缺失无法定档",
            "solution_readability": "未提供但可定档",
            "missing_information": ["题干曲线纵轴和关键数据不可读"],
            "conflict_description": "无",
        }
        raw["task_graph"] = {"nodes": [], "edges": []}
        raw["boundary_review"] = {
            "lower_level": "无法判断",
            "why_not_lower": "关键数据缺失，无法比较。",
            "upper_level": "无法判断",
            "why_not_higher": "关键数据缺失，无法比较。",
        }
        raw["difficulty_level"] = None
        with self.assertRaises(UnrateableInputError):
            validate_and_prepare_result(raw)

    def test_extra_feature_object_is_rejected(self) -> None:
        raw = valid_result()
        raw["features"] = {"reasoning_depth": "2-3层"}
        with self.assertRaisesRegex(DecoupledSchemaError, "extra"):
            validate_and_prepare_result(raw)


if __name__ == "__main__":
    unittest.main()
