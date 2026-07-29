from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chemistry_a2_full_audit import apply_a2_full_audit  # noqa: E402
from chemistry_two_pass_taskgraph_schema import validate_rating, validate_reconstruction  # noqa: E402


TASK_FULL = ROOT / "prompts" / "0730初中化学任务重建提示词_v6_decoupled_taskgraph_a2_full.txt"
RATING_FULL = ROOT / "prompts" / "0730初中化学难度打标提示词_v6_decoupled_taskgraph_a2_full.txt"
TASK_SHORT = ROOT / "prompts" / "0730初中化学任务重建提示词_v6_decoupled_taskgraph_a2.txt"
RATING_SHORT = ROOT / "prompts" / "0730初中化学难度打标提示词_v6_decoupled_taskgraph_a2.txt"
RUNNER_SHORT = ROOT / "src" / "chemistry_difficulty_rating_0730_v6_decoupled_taskgraph_a2_with_cache.py"
V6_PROMPT = ROOT / "prompts" / "0730初中化学难度打标提示词_v5_2_evidence15_v6.txt"
A1_PROMPT = ROOT / "prompts" / "0730初中化学难度打标提示词_v6_decoupled_taskgraph_a1.txt"
RUNNER = ROOT / "src" / "chemistry_difficulty_rating_0730_v6_decoupled_taskgraph_a2_full_with_cache.py"


def execute_prompt(path: Path) -> dict:
    namespace = {"__file__": str(path.resolve())}
    exec(path.read_text(encoding="utf-8"), namespace)
    return namespace


def reconstruction() -> dict:
    return validate_reconstruction({
        "input_assessment": {
            "can_reconstruct": True,
            "stem_readability": "完整",
            "solution_readability": "完整",
            "missing_information": [],
            "conflict_description": "无",
        },
        "reconstructed_question": {
            "complete_task_summary": "应用一条显性规则判断反应。",
            "subquestions": ["判断锌能否与稀盐酸反应"],
            "source_resolution": "文字信息完整，无冲突。",
        },
        "rated_target": {
            "scope": "整题",
            "task": "应用金属活动性顺序判断反应",
            "included_prerequisites": [],
            "selection_reason": "本题只有一个任务。",
        },
        "task_graph": {
            "nodes": [{
                "id": "N1", "subquestion": "整题", "primary_kind": "rule_application",
                "tags": ["reaction"], "counts_as_decision": True,
                "action": "应用金属活动性顺序判断锌能否与稀盐酸反应",
                "output": "锌能与稀盐酸反应",
                "source_evidence": "锌位于氢前，盐酸足量",
            }],
            "edges": [],
            "shared_models": [],
        },
        "audit_attributes": {
            "question_relation": "无多问",
            "visual_role": "无实质视觉信息",
            "reaction_structure": "单一直接反应",
            "experiment_structure": "无实验任务",
            "graph_table_structure": "无图表任务",
            "calculation_structure": "无定量任务",
            "information_transfer": "课内直接原型",
        },
    })


def raw_rating(level: str) -> dict:
    return {
        "difficulty_level": level,
        "rating_dimensions": {
            "dependency_demand": "单一显性应用",
            "model_demand": "单一显性规则",
            "evidence_constraint_demand": "单一显性条件",
            "quantitative_demand": "无定量",
            "transfer_demand": "课内原型",
        },
        "boundary_review": {
            "why_not_lower": "需要自主应用金属活动性顺序。",
            "why_not_higher": "只有一个显性规则，没有连续依赖。",
        },
        "decisive_reason": "单一规则应用。",
        "task_graph_review": {"status": "一致", "explanation": "任务图完整。"},
        "special_anchor": {"type": "无", "evidence": "不涉及特殊锚点。"},
    }


class FullPromptTests(unittest.TestCase):
    def test_task_prompt_is_level_blind_and_has_neutral_examples(self) -> None:
        namespace = execute_prompt(TASK_FULL)
        prefix = namespace["TASK_RECONSTRUCTION_PROMPT_PREFIX"]
        for level in ("送分题", "基础题", "中等题", "拔高题", "压轴题"):
            self.assertNotIn(level, prefix)
        self.assertGreaterEqual(prefix.count("【中性示例"), 8)
        self.assertIn("共享模型但无答案复用", prefix)
        self.assertGreater(len(prefix), len(TASK_SHORT.read_text(encoding="utf-8")))

    def test_rating_prompt_has_full_boundaries_and_18_examples(self) -> None:
        namespace = execute_prompt(RATING_FULL)
        prefix = namespace["DIFFICULTY_RATING_PROMPT_PREFIX"]
        self.assertEqual(prefix.count("【例题"), 18)
        for text in (
            "纵向依赖D与整题任务负担B分离",
            "送分题与基础题",
            "基础题与中等题",
            "中等题与拔高题",
            "拔高题与压轴题",
            "后处理边界",
        ):
            self.assertIn(text, prefix)
        self.assertNotIn("Evidence-15全部输出字段", prefix)
        self.assertGreater(
            len(prefix),
            2 * len(RATING_SHORT.read_text(encoding="utf-8")),
        )

    def test_wrapper_uses_composed_prompts_and_audit_only(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn("load_composed_prompt", source)
        self.assertIn("apply_a2_full_audit", source)
        self.assertIn("automatic level changes: disabled", source)

    def test_control_prompts_are_unchanged(self) -> None:
        self.assertEqual(
            hashlib.sha256(V6_PROMPT.read_bytes()).hexdigest().upper(),
            "12689B324181F73D2F454FFFB7EEA425197787CA001D42E05D9CAC1B57381B5B",
        )
        self.assertEqual(
            hashlib.sha256(A1_PROMPT.read_bytes()).hexdigest().upper(),
            "94E8F346A6F9C49664811FDBE8919594FF345DB02F6C015103FADEBC787943B8",
        )
        self.assertEqual(
            hashlib.sha256(TASK_SHORT.read_bytes()).hexdigest().upper(),
            "2FA70C6CD05FD0F73C92655CA1F8F746C2AE67760BED13C1D7A397368E083D5F",
        )
        self.assertEqual(
            hashlib.sha256(RATING_SHORT.read_bytes()).hexdigest().upper(),
            "DB3A13C44175C068CE0EB18372CAFF318CE780EBEE9759BAABFCCEB4CBEF9940",
        )
        self.assertEqual(
            hashlib.sha256(RUNNER_SHORT.read_bytes()).hexdigest().upper(),
            "1770BF8F2D7386F93E304260D29B7C1C2CD2AE1EAB090D0EE4D9BDB47AE3BF50",
        )


class AuditOnlyTests(unittest.TestCase):
    def test_low_structure_overrating_is_flagged_but_not_changed(self) -> None:
        rec = reconstruction()
        rating = validate_rating(raw_rating("中等题"), rec)
        audited = apply_a2_full_audit(rating, rec)
        self.assertEqual(audited["difficulty_level"], "中等题")
        self.assertEqual(audited["postprocess_original_level"], "中等题")
        self.assertEqual(audited["postprocess_final_level"], "中等题")
        self.assertFalse(audited["automatic_level_change_applied"])
        self.assertIn(
            "low_structure_overrating_candidate",
            [flag["code"] for flag in audited["a2_full_audit_flags"]],
        )

    def test_hard_without_edge_is_flagged_but_not_changed(self) -> None:
        rec = reconstruction()
        rating = validate_rating(raw_rating("拔高题"), rec)
        audited = apply_a2_full_audit(rating, rec)
        self.assertEqual(audited["difficulty_level"], "拔高题")
        self.assertIn(
            "hard_without_decisive_edge_candidate",
            [flag["code"] for flag in audited["a2_full_audit_flags"]],
        )

    def test_fixed_group_anchor_exempts_single_chain_final_flag(self) -> None:
        rec = reconstruction()
        raw = raw_rating("压轴题")
        raw["special_anchor"] = {
            "type": "固定基团教师锚点",
            "evidence": "共同硫酸根不变，整体剩余质量按固定组成拆分。",
        }
        rating = validate_rating(raw, rec)
        audited = apply_a2_full_audit(rating, rec)
        codes = [flag["code"] for flag in audited["a2_full_audit_flags"]]
        self.assertNotIn("final_without_coupling_candidate", codes)
        self.assertEqual(audited["difficulty_level"], "压轴题")

    def test_audit_does_not_mutate_input(self) -> None:
        rec = reconstruction()
        rating = validate_rating(raw_rating("中等题"), rec)
        before = copy.deepcopy(rating)
        apply_a2_full_audit(rating, rec)
        self.assertEqual(rating, before)


if __name__ == "__main__":
    unittest.main()
