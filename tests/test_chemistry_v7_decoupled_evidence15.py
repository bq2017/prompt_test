from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from chemistry_postprocess_v7_decoupled import (  # noqa: E402
    FEATURE_VALUES,
    LEVEL_INDEX,
    _base_result,
    postprocess_chemistry_difficulty,
    run_self_tests,
)


PROMPT = ROOT / "prompts" / "0730初中化学难度打标提示词_v7_decoupled_evidence15.txt"
POSTPROCESSOR = SRC / "chemistry_postprocess_v7_decoupled.py"
RUNNER = SRC / "chemistry_difficulty_rating_0730_v7_decoupled_evidence15_with_cache.py"
V6_PROMPT = ROOT / "prompts" / "0730初中化学难度打标提示词_v5_2_evidence15_v6.txt"


def normalized_text_sha256(path: Path, *, strip_final_newline: bool = False) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if strip_final_newline:
        text = text.rstrip("\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def load_prompt() -> tuple[str, str]:
    namespace = {"__file__": str(PROMPT)}
    exec(compile(PROMPT.read_text(encoding="utf-8"), str(PROMPT), "exec"), namespace)
    return namespace["DIFFICULTY_RATING_PROMPT_PREFIX"], namespace["DIFFICULTY_RATING_PROMPT_SUFFIX"]


class PromptContractTests(unittest.TestCase):
    def test_supplied_files_are_preserved(self) -> None:
        self.assertEqual(
            normalized_text_sha256(PROMPT),
            "AE78A37F0C87D1803DDB48EF9D3E9C79E7EDD51BED5CB9018E862E3C81A1532C",
        )
        self.assertEqual(
            normalized_text_sha256(POSTPROCESSOR),
            "8054E2F90135346D9EC9A66B01CD4E1471D365487EE125DF81A6378162C841D4",
        )

    def test_prompt_freezes_level_before_exact_evidence15(self) -> None:
        prefix, suffix = load_prompt()
        self.assertGreater(len(prefix), 19000)
        self.assertIn("冻结`difficulty_level`", prefix)
        self.assertIn("后置填写Evidence-15", prefix)
        self.assertIn("difficulty_level`必须出现在`features`之前", prefix)
        self.assertNotIn('"task_graph"', prefix)
        for field in FEATURE_VALUES:
            self.assertIn(f'"{field}"', prefix)
        self.assertEqual(len(FEATURE_VALUES), 15)
        self.assertIn("冻结档位后再填写15个Evidence-15", suffix)

    def test_v6_control_prompt_is_unchanged(self) -> None:
        self.assertEqual(
            normalized_text_sha256(V6_PROMPT),
            "AE50AA0FEAFD170651975728FB54C086A11C782F8F4B1B42B078FBF97976B871",
        )


class PostprocessTests(unittest.TestCase):
    def test_supplied_self_tests(self) -> None:
        run_self_tests()

    def test_prompt_only_preserves_level(self) -> None:
        result = postprocess_chemistry_difficulty(_base_result("基础题"), profile="prompt_only")
        self.assertEqual(result["difficulty_level_raw"], "基础题")
        self.assertEqual(result["difficulty_level"], "基础题")
        self.assertEqual(result["postprocess_actions"], [])

    def test_conservative_rule_is_adjacent_and_single(self) -> None:
        result = postprocess_chemistry_difficulty(_base_result("基础题"), profile="conservative_v1")
        self.assertEqual(result["difficulty_level_raw"], "基础题")
        self.assertEqual(result["difficulty_level"], "送分题")
        self.assertEqual(len(result["postprocess_actions"]), 1)
        self.assertEqual(
            abs(LEVEL_INDEX[result["difficulty_level_raw"]] - LEVEL_INDEX[result["difficulty_level"]]),
            1,
        )

    def test_runner_bridges_raw_level_to_evaluator(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn('prepared["postprocess_original_level"] = raw_level', source)
        self.assertIn('prepared["postprocess_final_level"] = final_level', source)
        self.assertIn('profile="prompt_only"', source)
        self.assertIn("POSTPROCESS_PROFILE", source)
        self.assertIn('CHEMISTRY_V7_CONCURRENCY", "30"', source)


if __name__ == "__main__":
    unittest.main()
