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
    UnrateableInputError,
    apply_observe_only_audit,
    validate_and_prepare_compact_result,
)
from evaluate_chemistry_difficulty import extract_prediction  # noqa: E402


PROMPT = ROOT / "prompts" / "0730初中化学难度打标提示词_v6_single_pass_compact_a3.txt"
RUNNER = ROOT / "src" / "chemistry_difficulty_rating_0730_v6_single_pass_compact_a3_with_cache.py"
RUN_SCRIPT = ROOT / "tools" / "run_chemistry_v6_single_pass_compact_a3_teacher0724_591.sh"
V6_PROMPT = ROOT / "prompts" / "0730初中化学难度打标提示词_v5_2_evidence15_v6.txt"
A1_PROMPT = ROOT / "prompts" / "0730初中化学难度打标提示词_v6_decoupled_taskgraph_a1.txt"
A2_TASK_PROMPT = ROOT / "prompts" / "0730初中化学任务重建提示词_v6_decoupled_taskgraph_a2.txt"
A2_RATING_PROMPT = ROOT / "prompts" / "0730初中化学难度打标提示词_v6_decoupled_taskgraph_a2.txt"
A2_RUNNER = ROOT / "src" / "chemistry_difficulty_rating_0730_v6_decoupled_taskgraph_a2_with_cache.py"


def normalized_text_sha256(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def load_runtime_prompt() -> tuple[str, str]:
    namespace = {"__file__": str(PROMPT)}
    exec(compile(PROMPT.read_text(encoding="utf-8"), str(PROMPT), "exec"), namespace)
    return (
        namespace["DIFFICULTY_RATING_PROMPT_PREFIX"],
        namespace["DIFFICULTY_RATING_PROMPT_SUFFIX"],
    )


def compact_rating() -> dict:
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
        "task_structure": {
            "question_relation": "多问存在结果或任务链依赖",
            "shared_model": {
                "present": True,
                "description": "各问共同依赖同一流程中的物质去向模型",
            },
            "decisive_dependencies": [
                {
                    "from": "判断过滤后滤液中的溶质组成",
                    "to": "确定后续可发生的反应并写方程式",
                    "basis": "前一步得到的实际反应物集合直接限定后一步反应",
                }
            ],
            "structure_summary": "一个共享流程模型内存在一条常规结果复用链。",
        },
        "rating_dimensions": {
            "dependency_demand": "常规连续模型",
            "model_demand": "完整常规模型",
            "evidence_constraint_demand": "常规联合证据或约束",
            "quantitative_demand": "无定量",
            "transfer_demand": "课内原型",
        },
        "boundary_review": {
            "why_not_lower": "需要先确定滤液组成，再确定可发生反应。",
            "why_not_higher": "不存在改变常规路径的过量、竞争或陌生迁移。",
        },
        "decisive_reason": "最高难任务是一条常规结果复用链，模型明确但没有高阶卡点。",
        "special_anchor": {"type": "无", "evidence": "不涉及固定基团特殊教师口径。"},
        "difficulty_level": "中等题",
    }


class PromptAndArchitectureTests(unittest.TestCase):
    def test_prompt_keeps_domain_calibration_but_compacts_output(self) -> None:
        prefix, suffix = load_runtime_prompt()
        self.assertGreater(len(prefix), 14000)
        self.assertGreaterEqual(prefix.count("【例题"), 18)
        for heading in ("反应与组分关系", "实验与证据", "图像与表格", "定量模型", "陌生信息迁移与表征转换"):
            self.assertIn(heading, prefix)
        self.assertIn("最多保留2条", prefix)
        self.assertIn("单调用", prefix)
        self.assertIn("紧凑JSON对象", suffix)
        contract = prefix[prefix.index("## 十二、紧凑输出合同"):]
        self.assertNotIn('"nodes"', contract)
        self.assertNotIn('"edges"', contract)
        self.assertNotIn("Evidence-15", contract)

    def test_runner_is_one_stage_and_exposes_concurrency(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        script = RUN_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('STAGE = "compact_single_pass_rating"', source)
        self.assertIn("CHEMISTRY_A3_CONCURRENCY", source)
        self.assertIn("single_pass_compact_architecture", source)
        self.assertIn("automatic_level_change_applied", source)
        self.assertNotIn('STAGE = "task_reconstruction"', source)
        self.assertNotIn('STAGE = "difficulty_rating"', source)
        self.assertIn('RUN_ARGS+=(-n "$NUM")', script)

    def test_control_versions_are_unchanged(self) -> None:
        expected = {
            V6_PROMPT: "AE50AA0FEAFD170651975728FB54C086A11C782F8F4B1B42B078FBF97976B871",
            A1_PROMPT: "94E8F346A6F9C49664811FDBE8919594FF345DB02F6C015103FADEBC787943B8",
            A2_TASK_PROMPT: "2FA70C6CD05FD0F73C92655CA1F8F746C2AE67760BED13C1D7A397368E083D5F",
            A2_RATING_PROMPT: "DB3A13C44175C068CE0EB18372CAFF318CE780EBEE9759BAABFCCEB4CBEF9940",
            A2_RUNNER: "1770BF8F2D7386F93E304260D29B7C1C2CD2AE1EAB090D0EE4D9BDB47AE3BF50",
        }
        for path, digest in expected.items():
            self.assertEqual(normalized_text_sha256(path), digest, path.name)


class CompactSchemaTests(unittest.TestCase):
    def test_valid_contract_derives_neighbors(self) -> None:
        value = validate_and_prepare_compact_result(compact_rating())
        self.assertEqual(value["difficulty_level"], "中等题")
        self.assertEqual(value["boundary_review"]["lower_level"], "基础题")
        self.assertEqual(value["boundary_review"]["upper_level"], "拔高题")
        self.assertEqual(len(value["task_structure"]["decisive_dependencies"]), 1)

    def test_more_than_two_dependencies_is_rejected(self) -> None:
        raw = compact_rating()
        raw["task_structure"]["decisive_dependencies"] *= 3
        with self.assertRaises(CompactSchemaError):
            validate_and_prepare_compact_result(raw)

    def test_extra_full_graph_is_rejected(self) -> None:
        raw = compact_rating()
        raw["task_graph"] = {"nodes": [], "edges": []}
        with self.assertRaisesRegex(CompactSchemaError, "extra"):
            validate_and_prepare_compact_result(raw)

    def test_unrateable_contract_is_explicit(self) -> None:
        raw = compact_rating()
        raw["input_assessment"] = {
            "can_rate": False,
            "stem_readability": "关键缺失无法定档",
            "solution_readability": "未提供但题面可独立定档",
            "missing_information": ["曲线纵轴数值不可读"],
            "conflict_description": "无",
        }
        for field in ("rated_target", "task_structure", "rating_dimensions", "boundary_review", "special_anchor", "difficulty_level"):
            raw[field] = None
        raw["decisive_reason"] = "关键曲线数值缺失，无法确定反应阶段。"
        with self.assertRaises(UnrateableInputError):
            validate_and_prepare_compact_result(raw)

    def test_audit_flags_never_change_level(self) -> None:
        raw = compact_rating()
        raw["difficulty_level"] = "拔高题"
        raw["task_structure"]["shared_model"] = {"present": False, "description": "无"}
        raw["task_structure"]["decisive_dependencies"] = []
        raw["rating_dimensions"] = {
            "dependency_demand": "直接识别",
            "model_demand": "无需模型",
            "evidence_constraint_demand": "无证据约束任务",
            "quantitative_demand": "无定量",
            "transfer_demand": "课内原型",
        }
        value = apply_observe_only_audit(validate_and_prepare_compact_result(raw))
        self.assertGreaterEqual(value["a3_audit_flag_count"], 1)
        self.assertEqual(value["postprocess_original_level"], "拔高题")
        self.assertEqual(value["postprocess_final_level"], "拔高题")
        self.assertEqual(value["difficulty_level"], "拔高题")
        self.assertFalse(value["automatic_level_change_applied"])
        item = {"difficulty_rating": value}
        self.assertEqual(extract_prediction(item, "pre-postprocess"), ("拔高题", 4))
        self.assertEqual(extract_prediction(item, "final"), ("拔高题", 4))


if __name__ == "__main__":
    unittest.main()
