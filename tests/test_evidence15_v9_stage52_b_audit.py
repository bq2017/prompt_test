from __future__ import annotations

import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from analyze_stage52_b_repeats import promotion_block
from chemistry_stage52_b_schema import (
    SCHEMA_VERSION,
    Stage52BAuditError,
    derive_composite_burden,
    failed_gates,
    validate_b_audit,
)
from extract_stage52_b_candidates import extract_candidate, raw_stage5_level


def valid_audit(**overrides):
    audit = {
        "constructed_representation": "由多条题面信息构造候选状态与约束系统M",
        "representation_nonroutine": True,
        "operation_x": "生成并分类候选状态",
        "operation_y": "用独立证据排除候选并闭合结论",
        "different_causal_roles": True,
        "same_hardest_task": True,
        "independently_solvable": False,
        "answer_splicing": False,
        "mechanical_tail": False,
        "delete_x_effect": "closure_breaks",
        "delete_y_effect": "closure_breaks",
        "reason": "M、X、Y共同服务于同一最高难任务，删除任一均无法闭合。",
    }
    audit.update(overrides)
    return audit


def stage5_item(raw_level: str = "拔高题"):
    return {
        "question_id": "q1",
        "stem": "题目",
        "options": ["A", "B"],
        "analysis": "解析",
        "sub_questions": [],
        "difficulty": 5,
        "difficulty_rating_raw": {
            "difficulty_level": raw_level,
            "features": {"secret": "不得传入B"},
            "reasoning": {"why_not_higher": "不得传入B"},
        },
        "difficulty_rating": {
            "difficulty_level": raw_level,
            "postprocess_original_level": raw_level,
            "reasoning": {"core_basis": "不得传入B"},
        },
    }


class Stage52BAuditContractTests(unittest.TestCase):
    def test_prompt_is_a_b_only_auditor(self):
        namespace = runpy.run_path(
            str(ROOT / "prompts" / "evidence15_v9_stage52_b_audit_prompt.txt")
        )
        prompt = namespace["DIFFICULTY_RATING_PROMPT_PREFIX"]
        for required in (
            "同一最高难任务",
            "现场构造中间模型M",
            "两个不可约的高阶因果功能X、Y",
            "不可拆分且不是机械长尾",
            "不要自行输出composite_burden或最终档位",
        ):
            self.assertIn(required, prompt)
        for forbidden_support in (
            "综合性强、篇幅较大",
            "普通数值传递",
            "两个独立小问",
        ):
            self.assertIn(forbidden_support, prompt)

    def test_schema_accepts_exact_valid_audit(self):
        checked = validate_b_audit(valid_audit())
        self.assertEqual(tuple(checked), tuple(valid_audit()))
        self.assertTrue(derive_composite_burden(checked))
        self.assertEqual(failed_gates(checked), [])

    def test_schema_rejects_extra_fields_and_string_booleans(self):
        with self.assertRaises(Stage52BAuditError):
            validate_b_audit(valid_audit(composite_burden=True))
        with self.assertRaises(Stage52BAuditError):
            validate_b_audit(valid_audit(answer_splicing="false"))

    def test_every_gate_can_veto_trigger(self):
        vetoes = {
            "representation_nonroutine": False,
            "different_causal_roles": False,
            "same_hardest_task": False,
            "independently_solvable": True,
            "answer_splicing": True,
            "mechanical_tail": True,
            "delete_x_effect": "only_loses_independent_answer",
            "delete_y_effect": "no_material_change",
        }
        for field, value in vetoes.items():
            with self.subTest(field=field):
                audit = valid_audit(**{field: value})
                self.assertFalse(derive_composite_burden(audit))
                self.assertTrue(failed_gates(audit))

    def test_raw_level_never_uses_stale_top_level_difficulty(self):
        item = stage5_item("拔高题")
        item["difficulty"] = 1
        self.assertEqual(raw_stage5_level(item), "拔高题")

    def test_raw_level_sources_must_agree(self):
        item = stage5_item("拔高题")
        item["difficulty_rating_raw"]["difficulty_level"] = "压轴题"
        with self.assertRaises(ValueError):
            raw_stage5_level(item)

    def test_candidate_is_allowlisted_and_blind_to_stage5_reasoning(self):
        candidate = extract_candidate(stage5_item())
        self.assertEqual(candidate["question_id"], "q1")
        for forbidden in (
            "difficulty",
            "difficulty_rating",
            "difficulty_rating_raw",
            "features",
            "reasoning",
            "core_basis",
            "why_not_higher",
        ):
            self.assertNotIn(forbidden, candidate)

    def test_protocol_net_subtracts_all_non5_promotions(self):
        labels = {"q5a": 5, "q5b": 5, "q4": 4, "q3": 3}
        s = {qid: 4 for qid in labels}
        t = {qid: 5 for qid in labels}
        report, promoted = promotion_block(labels, s, t)
        self.assertEqual(len(promoted), 4)
        self.assertEqual(report["teacher5_rescued"], 2)
        self.assertEqual(report["all_non5_promoted"], 2)
        self.assertEqual(report["teacher4_to_5"], 1)
        self.assertEqual(report["teacher_le3_to_5_severe"], 1)
        self.assertEqual(report["b_protocol_net_benefit"], 0)
        self.assertEqual(report["exact_match_delta"], 1)
        self.assertEqual(report["average_absolute_error_change"], 0.0)

    def test_schema_version_is_frozen(self):
        self.assertEqual(SCHEMA_VERSION, "stage52_b_audit_v1")

    def test_archived_preprotocol_files_are_marked_not_for_run(self):
        readme = (
            ROOT
            / "archive"
            / "stage52_preprotocol_draft_20260730"
            / "README.md"
        ).read_text(encoding="utf-8")
        self.assertIn("not_for_run", readme)


if __name__ == "__main__":
    unittest.main()
