from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chemistry_evidence15_v9_schema import (
    Core12SchemaError,
    apply_data_aware_boundary_rules,
    apply_targeted_evidence_rules,
    apply_safe_adjacent_level_change,
    validate_and_prepare_result,
)


RUNNER_PATH = ROOT / "src" / "chemistry_difficulty_rating_evidence15_v9_with_cache.py"


class _AsyncFile:
    def __init__(self, path, mode, encoding="utf-8"):
        self.path = path
        self.mode = mode
        self.encoding = encoding
        self.handle = None

    async def __aenter__(self):
        self.handle = open(self.path, self.mode, encoding=self.encoding)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.handle.close()

    async def write(self, value):
        return self.handle.write(value)


def _install_import_stubs() -> None:
    if "aiofiles" not in sys.modules:
        aiofiles = types.ModuleType("aiofiles")
        aiofiles.open = lambda path, mode, encoding="utf-8": _AsyncFile(path, mode, encoding)
        sys.modules["aiofiles"] = aiofiles
    if "aiohttp" not in sys.modules:
        aiohttp = types.ModuleType("aiohttp")
        aiohttp.ClientSession = type("ClientSession", (), {})
        aiohttp.ClientTimeout = type("ClientTimeout", (), {"__init__": lambda self, **kwargs: None})
        aiohttp.ClientError = type("ClientError", (Exception,), {})
        sys.modules["aiohttp"] = aiohttp
    if "dotenv" not in sys.modules:
        dotenv = types.ModuleType("dotenv")
        dotenv.load_dotenv = lambda: None
        sys.modules["dotenv"] = dotenv
    if "tqdm" not in sys.modules:
        tqdm_package = types.ModuleType("tqdm")
        tqdm_asyncio = types.ModuleType("tqdm.asyncio")
        tqdm_asyncio.tqdm = lambda *args, **kwargs: None
        sys.modules["tqdm"] = tqdm_package
        sys.modules["tqdm.asyncio"] = tqdm_asyncio


_install_import_stubs()


def load_runner():
    spec = importlib.util.spec_from_file_location("evidence15_v9_production_runner", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = load_runner()


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
            "core_basis": "纵向链深D约3个有效化学决策，整题任务负担B为单一常规模型。",
            "hard_point": "常规关系建立。",
            "why_not_lower": "超过一次应用。",
            "why_not_higher": "没有高阶卡点。",
        },
        "difficulty_level": "中等题",
    }


def mocked_call(
    result: dict,
    *,
    parse_error: str = "",
    time_use: float = 1.0,
    tokens: int = 10,
    http_retries: int = 0,
):
    return (
        copy.deepcopy(result),
        json.dumps(result, ensure_ascii=False) if result else "{\"truncated\"",
        parse_error,
        time_use,
        tokens,
        tokens,
        tokens * 2,
        {"http_retry_count": http_retries, "image_input_requested": True, "image_input_used": True},
    )


class Core12SchemaTests(unittest.TestCase):
    def test_question_id_is_not_sent_to_prompt(self) -> None:
        data = {
            "question_id": "3409521577253249024",
            "stem": "测试题干",
            "options": "A.测试",
            "analysis": "测试解析",
            "stem_pic_url": "https://example.invalid/stem.png",
            "analysis_pic_url": "https://example.invalid/analysis.png",
            "sub_questions": [],
        }
        image_content = json.dumps(RUNNER.build_user_content(data, True), ensure_ascii=False)
        text_content = str(RUNNER.build_user_content(data, False))
        self.assertNotIn(data["question_id"], image_content)
        self.assertNotIn(data["question_id"], text_content)
        self.assertIn("测试题干", text_content)

    def test_legal_output_passes_without_level_change(self) -> None:
        source = valid_result()
        prepared = validate_and_prepare_result(source, {"sub_questions": []})
        self.assertEqual(prepared["difficulty_level"], source["difficulty_level"])
        self.assertFalse(prepared["automatic_level_change_applied"])
        self.assertEqual(prepared["postprocess_profile"], "evidence15_v9_schema_only")
        self.assertEqual(prepared["feature_schema_version"], "chemistry_evidence15_v9_stage2")
        self.assertEqual(prepared["postprocess_original_level"], source["difficulty_level"])

    def test_entry_operation_does_not_force_total_reasoning_depth(self) -> None:
        source = valid_result()
        source["features"]["entry_operation"] = "自主选择一条规则"
        source["features"]["reasoning_depth"] = "4-5层"
        prepared = validate_and_prepare_result(source, {})
        self.assertEqual(prepared["features"]["reasoning_depth"], "4-5层")

    def test_internal_dependency_does_not_require_multiple_questions(self) -> None:
        source = valid_result()
        source["features"]["task_rule_breadth"] = "存在结果或任务链依赖"
        source["features"]["subquestion_dependency"] = "无多问"
        prepared = validate_and_prepare_result(source, {})
        self.assertEqual(prepared["features"]["subquestion_dependency"], "无多问")

    def test_source_subquestion_shape_no_longer_rejects_valid_json(self) -> None:
        source = valid_result()
        prepared = validate_and_prepare_result(source, {"sub_questions": []})
        prepared_with_subquestions = validate_and_prepare_result(source, {"sub_questions": [{}, {}]})
        self.assertEqual(prepared["difficulty_level"], "中等题")
        self.assertEqual(prepared_with_subquestions["difficulty_level"], "中等题")

    def test_top_level_contract_is_exact(self) -> None:
        invalid = valid_result()
        invalid["question_structure"] = "复合题"
        with self.assertRaisesRegex(Core12SchemaError, "顶层字段不固定"):
            validate_and_prepare_result(invalid, {})

    def test_coarse_interval_must_contain_final_level(self) -> None:
        invalid = valid_result()
        invalid["coarse_difficulty"] = "拔高/压轴区间（4-5档）"
        with self.assertRaisesRegex(Core12SchemaError, "不在同一相邻边界"):
            validate_and_prepare_result(invalid, {})

        basic = valid_result()
        basic["difficulty_level"] = "基础题"
        basic["coarse_difficulty"] = "送分/基础区间（1-2档）"
        validate_and_prepare_result(basic, {})
        basic["coarse_difficulty"] = "基础/中等区间（2-3档）"
        validate_and_prepare_result(basic, {})

    def test_safe_change_is_adjacent_only(self) -> None:
        prepared = validate_and_prepare_result(valid_result(), {"sub_questions": []})
        changed = apply_safe_adjacent_level_change(prepared, "拔高题", "离线批准的Core-12规则")
        self.assertEqual(changed["difficulty_level"], "拔高题")
        with self.assertRaisesRegex(Core12SchemaError, "相邻档"):
            apply_safe_adjacent_level_change(prepared, "压轴题", "越级")

    def test_targeted_rule_demotes_direct_recognition_only(self) -> None:
        source = valid_result()
        source["difficulty_level"] = "基础题"
        source["coarse_difficulty"] = "送分/基础区间（1-2档）"
        source["features"].update({
            "entry_operation": "直接检索",
            "task_rule_breadth": "单一规则或同类检索束",
            "visual_information_role": "无实质视觉信息",
            "reasoning_depth": "0层",
            "reasoning_direction": "直接识记",
            "knowledge_relation": "单一知识点",
            "representation_conversion": "无",
            "reaction_relation": "无反应关系",
            "constraint_complexity": "无约束",
            "evidence_relation": "无证据任务",
            "experiment_requirement": "无",
            "graph_table_requirement": "无",
            "calculation_model": "无",
            "unfamiliar_information_transfer": "课内直接原型",
            "subquestion_dependency": "无多问",
        })
        prepared = validate_and_prepare_result(source, {})
        changed = apply_targeted_evidence_rules(prepared)
        self.assertEqual(changed["difficulty_level"], "送分题")
        self.assertTrue(changed["automatic_level_change_applied"])

    def test_direct_demote_rejects_contradictory_high_features(self) -> None:
        source = valid_result()
        source["difficulty_level"] = "基础题"
        source["coarse_difficulty"] = "送分/基础区间（1-2档）"
        source["features"].update({
            "entry_operation": "直接检索",
            "task_rule_breadth": "单一规则或同类检索束",
            "visual_information_role": "无实质视觉信息",
            "reasoning_depth": "0层",
            "reasoning_direction": "直接识记",
            "knowledge_relation": "跨模块融合",
            "representation_conversion": "无",
            "reaction_relation": "无反应关系",
            "constraint_complexity": "多层嵌套约束",
            "evidence_relation": "无证据任务",
            "experiment_requirement": "无",
            "graph_table_requirement": "无",
            "calculation_model": "无",
            "unfamiliar_information_transfer": "完全陌生模型现场建立",
            "subquestion_dependency": "无多问",
        })
        prepared = validate_and_prepare_result(source, {})
        unchanged = apply_targeted_evidence_rules(prepared)
        self.assertEqual(unchanged["difficulty_level"], "基础题")
        self.assertFalse(unchanged["automatic_level_change_applied"])

    def test_empty_subquestion_metadata_is_not_described_as_zero_questions(self) -> None:
        text, fields = RUNNER.build_minimal_text_supplement({}, [])
        self.assertIn("未提供已拆分的小问信息", text)
        self.assertIn("不得把列表为空理解为本题没有小问", text)
        self.assertNotIn("共有0个小问", text)
        self.assertIn("structured_subquestions_not_provided", fields)

    def test_image_primary_defaults_and_prompt_path_are_safe(self) -> None:
        source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn('"CHEMISTRY_ENABLE_IMAGE_INPUT", "1"', source)
        self.assertTrue(RUNNER.DEFAULT_PROMPT.exists())
        self.assertEqual(RUNNER.DEFAULT_PROMPT, ROOT / "prompts" / "evidence15_v9_prompt.txt")

    def test_targeted_rule_promotes_complete_final_chain(self) -> None:
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
        prepared = validate_and_prepare_result(source, {})
        changed = apply_targeted_evidence_rules(prepared)
        self.assertEqual(changed["difficulty_level"], "压轴题")
        self.assertTrue(changed["automatic_level_change_applied"])

    def test_three_high_words_do_not_promote_without_high_depth(self) -> None:
        source = valid_result()
        source["features"].update({
            "entry_operation": "形成中间结论后应用",
            "reasoning_depth": "2-3层",
            "reasoning_direction": "正向推导",
            "constraint_complexity": "多个相互关联约束",
            "evidence_relation": "需要排除竞争解释",
            "experiment_requirement": "方案设计、评价或补充实验",
            "calculation_model": "口算或直接比例",
        })
        prepared = validate_and_prepare_result(source, {})
        unchanged = apply_targeted_evidence_rules(prepared)
        self.assertEqual(unchanged["difficulty_level"], "中等题")
        self.assertFalse(unchanged["automatic_level_change_applied"])

    def test_dataaware_graph_quantitative_cardpoint_promotes_one_level(self) -> None:
        source = valid_result()
        source["features"].update({
            "entry_operation": "形成中间结论后应用",
            "visual_information_role": "决定模型、阶段或任务链",
            "reasoning_depth": "2-3层",
            "knowledge_relation": "同模块深度关联",
            "representation_conversion": "两类表征连续转换",
            "constraint_complexity": "多个相互关联约束",
            "graph_table_requirement": "拐点、平台或分段反推",
            "calculation_model": "单一方程式或关系式",
            "experiment_requirement": "控制变量、现象解释或数据归纳",
        })
        prepared = validate_and_prepare_result(source, {})
        changed = apply_data_aware_boundary_rules(
            prepared,
            {"stem": "根据反应曲线的平台和拐点选择数据，计算溶质质量分数。"},
        )
        self.assertEqual(changed["difficulty_level"], "拔高题")
        self.assertEqual(changed["postprocess_original_level"], "中等题")
        self.assertEqual(changed["postprocess_actions"][0]["rule"], "dataaware_medium_to_hard")

    def test_dataaware_final_requires_explicit_task_dependency(self) -> None:
        source = valid_result()
        source["difficulty_level"] = "拔高题"
        source["coarse_difficulty"] = "拔高/压轴区间（4-5档）"
        source["features"].update({
            "entry_operation": "多节点连续决策",
            "task_rule_breadth": "共享模型但无结果依赖",
            "visual_information_role": "决定模型、阶段或任务链",
            "reasoning_depth": "4-5层",
            "reasoning_direction": "分类讨论或综合推导",
            "knowledge_relation": "多模块深度融合",
            "representation_conversion": "宏观-微观-符号-定量多重转换",
            "reaction_relation": "多反应连续转化",
            "constraint_complexity": "多个相互关联约束",
            "evidence_relation": "需要排除竞争解释",
            "experiment_requirement": "多阶段探究与定量误差",
            "graph_table_requirement": "拐点、平台或分段反推",
            "calculation_model": "单一守恒或多反应计算",
            "unfamiliar_information_transfer": "迁移后建立关系",
            "subquestion_dependency": "多问共享模型但无答案依赖",
        })
        prepared = validate_and_prepare_result(source, {})
        unchanged = apply_data_aware_boundary_rules(
            prepared,
            {"stem": "任务一设计实验，任务二分析过量反应，任务三根据曲线计算质量分数。"},
        )
        self.assertEqual(unchanged["difficulty_level"], "拔高题")
        self.assertFalse(unchanged["automatic_level_change_applied"])

        source["features"]["subquestion_dependency"] = "多问存在结果或任务链依赖"
        source["features"]["task_rule_breadth"] = "存在结果或任务链依赖"
        prepared = validate_and_prepare_result(source, {})
        changed = apply_data_aware_boundary_rules(
            prepared,
            {"stem": "任务一设计实验并排除干扰；任务二根据所得物质继续过量反应；任务三根据曲线计算质量分数。"},
        )
        self.assertEqual(changed["difficulty_level"], "压轴题")

    def test_dataaware_does_not_demote_from_missing_high_evidence(self) -> None:
        source = valid_result()
        source["difficulty_level"] = "拔高题"
        source["coarse_difficulty"] = "中等/拔高区间（3-4档）"
        source["features"].update({
            "entry_operation": "多节点连续决策",
            "task_rule_breadth": "存在结果或任务链依赖",
            "visual_information_role": "提供局部关系",
            "reasoning_depth": "2-3层",
            "reasoning_direction": "正向推导",
            "constraint_complexity": "多个相互关联约束",
            "evidence_relation": "多条清晰证据联合",
            "experiment_requirement": "控制变量、现象解释或数据归纳",
            "subquestion_dependency": "多问存在结果或任务链依赖",
        })
        prepared = validate_and_prepare_result(source, {})
        unchanged = apply_data_aware_boundary_rules(
            prepared,
            {"stem": "探究中和反应后取上层清液继续验证，并评价替代方案是否可行。"},
        )
        self.assertEqual(unchanged["difficulty_level"], "拔高题")
        self.assertFalse(unchanged["automatic_level_change_applied"])

    def test_dataaware_never_demotes_raw_final_level(self) -> None:
        source = valid_result()
        source["difficulty_level"] = "压轴题"
        source["coarse_difficulty"] = "拔高/压轴区间（4-5档）"
        prepared = validate_and_prepare_result(source, {})
        unchanged = apply_data_aware_boundary_rules(prepared, {"stem": "简短题干"})
        self.assertEqual(unchanged["difficulty_level"], "压轴题")
        self.assertFalse(unchanged["automatic_level_change_applied"])

    def test_evidence15_direct_same_rule_bundle_demotes_basic(self) -> None:
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
        changed = apply_data_aware_boundary_rules(
            validate_and_prepare_result(source, {}),
            {"stem": "根据图中六个安全标志分别写出名称。"},
        )
        self.assertEqual(changed["difficulty_level"], "送分题")
        self.assertEqual(changed["postprocess_actions"][0]["rule"], "evidence15_basic_to_easy")

    def test_evidence15_multiple_answer_rules_promote_easy(self) -> None:
        source = valid_result()
        source["difficulty_level"] = "送分题"
        source["coarse_difficulty"] = "送分/基础区间（1-2档）"
        source["features"].update({
            "entry_operation": "自主选择一条规则",
            "task_rule_breadth": "多个独立回答规则",
            "visual_information_role": "仅呈现或重复文字",
            "reasoning_depth": "1层",
            "reasoning_direction": "正向推导",
            "knowledge_relation": "同模块简单关联",
            "representation_conversion": "无",
            "reaction_relation": "无反应关系",
            "constraint_complexity": "无约束",
            "evidence_relation": "无证据任务",
            "experiment_requirement": "无",
            "graph_table_requirement": "无",
            "calculation_model": "无",
            "unfamiliar_information_transfer": "课内直接原型",
            "subquestion_dependency": "多问相互独立",
        })
        changed = apply_data_aware_boundary_rules(
            validate_and_prepare_result(source, {}),
            {"stem": "分别完成物质分类、化学用语书写和实验规范判断。"},
        )
        self.assertEqual(changed["difficulty_level"], "基础题")

    def test_evidence15_visual_layout_is_not_difficulty_evidence(self) -> None:
        source = valid_result()
        source["difficulty_level"] = "基础题"
        source["coarse_difficulty"] = "送分/基础区间（1-2档）"
        source["features"].update({
            "entry_operation": "直接检索",
            "task_rule_breadth": "单一规则或同类检索束",
            "visual_information_role": "仅呈现或重复文字",
            "reasoning_depth": "0层",
            "reasoning_direction": "直接识记",
            "knowledge_relation": "单一知识点",
            "representation_conversion": "无",
            "reaction_relation": "无反应关系",
            "constraint_complexity": "无约束",
            "evidence_relation": "无证据任务",
            "experiment_requirement": "无",
            "graph_table_requirement": "无",
            "calculation_model": "无",
            "unfamiliar_information_transfer": "课内直接原型",
            "subquestion_dependency": "无多问",
        })
        changed = apply_data_aware_boundary_rules(
            validate_and_prepare_result(source, {}),
            {"stem": "观察排版复杂的图片，直接指出仪器名称。"},
        )
        self.assertEqual(changed["difficulty_level"], "送分题")


class Core12RetryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def run_process(self, sequence, *, schema_retries=2, json_retries=2):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.jsonl"
            error = Path(tmp) / "err.jsonl"
            mock = AsyncMock(side_effect=sequence)
            with (
                patch.object(RUNNER, "call_model_with_cache", mock),
                patch.object(RUNNER, "MAX_SCHEMA_RETRIES", schema_retries),
                patch.object(RUNNER, "MAX_JSON_PARSE_RETRIES", json_retries),
                patch.object(RUNNER, "CORE12_POSTPROCESS_PROFILE", "evidence15_v9_schema_only"),
            ):
                await RUNNER.process_single_question(
                    {"question_id": "offline-test", "sub_questions": []},
                    None,
                    asyncio.Semaphore(1),
                    str(output),
                    str(error),
                    1,
                    10,
                )
            out_rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()] if output.exists() else []
            err_rows = [json.loads(line) for line in error.read_text(encoding="utf-8").splitlines()] if error.exists() else []
            return out_rows, err_rows, mock

    async def test_legal_output_one_pass(self) -> None:
        out, err, mock = await self.run_process([mocked_call(valid_result())])
        self.assertEqual(len(out), 1)
        self.assertFalse(err)
        self.assertEqual(mock.await_count, 1)
        self.assertEqual(out[0]["schema_retry_count"], 0)
        self.assertEqual(
            out[0]["input_structure_audit"]["structured_subquestions_status"],
            "not_provided",
        )
        self.assertFalse(out[0]["input_structure_audit"]["blocking"])

    async def test_missing_field_triggers_schema_retry_and_accumulates_usage(self) -> None:
        invalid = valid_result()
        invalid["features"].pop("reaction_relation")
        out, err, mock = await self.run_process([
            mocked_call(invalid, time_use=1.25, tokens=10, http_retries=2),
            mocked_call(valid_result(), time_use=2.5, tokens=20, http_retries=1),
        ])
        self.assertFalse(err)
        self.assertEqual(mock.await_count, 2)
        self.assertEqual(out[0]["schema_retry_count"], 1)
        self.assertIn("reaction_relation", out[0]["schema_validation_errors"][0])
        self.assertEqual(out[0]["api_time_use"], 3.75)
        self.assertEqual(out[0]["api_total_tokens"], 60)
        self.assertEqual(out[0]["http_retry_count"], 3)

    async def test_illegal_enum_triggers_schema_retry(self) -> None:
        invalid = valid_result()
        invalid["features"]["reasoning_direction"] = "综合推导"
        out, err, mock = await self.run_process([mocked_call(invalid), mocked_call(valid_result())])
        self.assertFalse(err)
        self.assertEqual(out[0]["schema_retry_count"], 1)
        self.assertIn("非法值", out[0]["schema_validation_errors"][0])

    async def test_truncated_json_triggers_parse_retry(self) -> None:
        out, err, mock = await self.run_process([
            mocked_call({}, parse_error="JSON解析失败: 截断"),
            mocked_call(valid_result()),
        ])
        self.assertFalse(err)
        self.assertEqual(out[0]["json_parse_retry_count"], 1)
        self.assertEqual(mock.await_count, 2)

    async def test_retry_exhaustion_writes_error(self) -> None:
        invalid = valid_result()
        invalid["features"].pop("reaction_relation")
        out, err, mock = await self.run_process(
            [mocked_call(invalid), mocked_call(invalid)],
            schema_retries=1,
        )
        self.assertFalse(out)
        self.assertEqual(len(err), 1)
        self.assertIn("schema校验重试耗尽", err[0]["rating_error"])
        self.assertEqual(err[0]["schema_retry_count"], 1)


if __name__ == "__main__":
    unittest.main()
