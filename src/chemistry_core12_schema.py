"""Compact chemistry Core-12 contract and targeted adjacent postprocessing.

The model contract contains only ``features``, ``coarse_difficulty``,
``reasoning`` and ``difficulty_level``.  Optional targeted rules use only the
strict Core-12 evidence object, never labels or question identifiers, and may
change at most one adjacent level with an auditable trace.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping


LEVELS = ("送分题", "基础题", "中等题", "拔高题", "压轴题")
COARSE_LEVELS = (
    "送分/基础区间（1-2档）",
    "基础/中等区间（2-3档）",
    "中等/拔高区间（3-4档）",
    "拔高/压轴区间（4-5档）",
)
ALLOWED_COARSE_BY_LEVEL = {
    "送分题": {"送分/基础区间（1-2档）"},
    "基础题": {"送分/基础区间（1-2档）", "基础/中等区间（2-3档）"},
    "中等题": {"基础/中等区间（2-3档）", "中等/拔高区间（3-4档）"},
    "拔高题": {"中等/拔高区间（3-4档）", "拔高/压轴区间（4-5档）"},
    "压轴题": {"拔高/压轴区间（4-5档）"},
}

CORE_FEATURE_VALUES: dict[str, tuple[str, ...]] = {
    "entry_operation": ("直接检索", "一次透明映射", "自主选择一条规则", "形成中间结论后应用", "多节点连续决策"),
    "task_rule_breadth": ("单一规则或同类检索束", "多个独立回答规则", "共享模型但无结果依赖", "存在结果或任务链依赖"),
    "visual_information_role": ("无实质视觉信息", "仅呈现或重复文字", "直接识图作答", "提供局部关系", "决定模型、阶段或任务链"),
    "reasoning_depth": ("0层", "1层", "2-3层", "4-5层", "6层及以上"),
    "reasoning_direction": ("直接识记", "正向推导", "逆向推导", "分类讨论或综合推导"),
    "knowledge_relation": ("单一知识点", "同模块简单关联", "同模块深度关联", "跨模块融合", "多模块深度融合"),
    "representation_conversion": ("无", "一次表征转换", "两类表征连续转换", "宏观-微观-符号-定量多重转换"),
    "reaction_relation": (
        "无反应关系",
        "单一直接反应",
        "2-3个并列或简单连续反应",
        "多反应连续转化",
        "先后、竞争或过量不足",
        "需要分情况判断的反应模型",
    ),
    "constraint_complexity": ("无约束", "单一约束", "多个相互关联约束", "多层嵌套约束"),
    "evidence_relation": (
        "无证据任务",
        "单一证据直接对应",
        "多条清晰证据联合",
        "需要排除竞争解释",
        "证据冲突、筛选或多层排除",
    ),
    "experiment_requirement": ("无", "基础操作或读数", "控制变量、现象解释或数据归纳", "方案设计、评价或补充实验", "多阶段探究与定量误差"),
    "graph_table_requirement": ("无", "直接读数", "多组比较归纳", "拐点、平台或分段反推", "多图表耦合建模"),
    "calculation_model": ("无", "口算或直接比例", "单一方程式或关系式", "单一守恒或多反应计算", "多重守恒、差量、联立或分类"),
    "unfamiliar_information_transfer": ("课内直接原型", "给定新信息直接应用", "迁移后建立关系", "完全陌生模型现场建立"),
    "subquestion_dependency": ("无多问", "多问相互独立", "多问共享模型但无答案依赖", "多问存在结果或任务链依赖"),
}
CORE_FEATURE_FIELDS = tuple(CORE_FEATURE_VALUES)

AUDIT_FEATURE_VALUES: dict[str, tuple[str, ...]] = {
    "knowledge_count": ("1个", "2-3个", "4个及以上"),
    "solution_operations": ("1-2个", "3-5个", "6-8个", "9-12个", "12个以上"),
    "equation_count": ("0个", "1个", "2-3个", "4个及以上"),
    "problem_structure": ("概念识记", "化学用语与分类", "实验基础", "实验探究", "物质鉴别除杂推断", "酸碱盐与金属", "工艺流程", "图像表格", "定量计算", "跨模块综合"),
    "information_carrier": ("纯文字", "简单示意图", "实验装置图", "流程图", "图像或表格", "多图表或多材料综合"),
}
AUDIT_FEATURE_FIELDS = tuple(AUDIT_FEATURE_VALUES)

BANNED_PRODUCTION_FIELDS = {
    "teacher_boundary_anchor",
    "five_level_redline_check",
    "knowledge_diff",
    "reality_question",
    "error_risk",
    "excess_deficiency",
    "interference_exclusion",
    "constraint_count",
}


class Core12SchemaError(ValueError):
    """Raised when a model result is unsafe for automatic postprocessing."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _require_exact_enum_object(
    raw: Any,
    schema: Mapping[str, tuple[str, ...]],
    object_name: str,
) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise Core12SchemaError(f"{object_name} 必须是JSON对象")
    expected = set(schema)
    actual = set(raw)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise Core12SchemaError(
            f"{object_name}字段不完整：missing={missing}, extra={extra}"
        )
    validated: dict[str, str] = {}
    for field, allowed in schema.items():
        value = _text(raw[field])
        if value not in allowed:
            raise Core12SchemaError(
                f"{object_name}.{field} 非法值 {value!r}；允许值={allowed}"
            )
        validated[field] = value
    return validated


def validate_core_features(raw: Any) -> dict[str, str]:
    return _require_exact_enum_object(raw, CORE_FEATURE_VALUES, "core_features")


def validate_features(raw: Any) -> dict[str, str]:
    """Validate the production Evidence-15 object and its core invariants."""
    features = _require_exact_enum_object(raw, CORE_FEATURE_VALUES, "features")
    entry = features["entry_operation"]
    depth = features["reasoning_depth"]
    if entry == "直接检索" and depth != "0层":
        raise Core12SchemaError("直接检索必须对应reasoning_depth=0层")
    if entry == "一次透明映射" and depth not in {"0层", "1层"}:
        raise Core12SchemaError("一次透明映射只能对应0层或1层")
    if entry == "自主选择一条规则" and depth != "1层":
        raise Core12SchemaError("自主选择一条规则必须对应reasoning_depth=1层")
    if entry in {"形成中间结论后应用", "多节点连续决策"} and depth in {"0层", "1层"}:
        raise Core12SchemaError("形成中间结论或连续决策不能填写0层/1层")
    breadth = features["task_rule_breadth"]
    dependency = features["subquestion_dependency"]
    if breadth == "存在结果或任务链依赖" and dependency != "多问存在结果或任务链依赖":
        raise Core12SchemaError("任务链依赖在task_rule_breadth与subquestion_dependency中必须一致")
    return features


def validate_audit_features(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    return _require_exact_enum_object(raw, AUDIT_FEATURE_VALUES, "audit_features")


def _map_legacy_reaction(features: Mapping[str, Any]) -> str:
    excess = _text(features.get("excess_deficiency"))
    reaction = _text(features.get("reaction_relation"))
    if excess in {"需要分情况讨论", "需要分情况判断"}:
        return "需要分情况判断的反应模型"
    if excess == "需要判断过量不足" or reaction in {"先后或竞争反应", "先后、竞争或过量不足"}:
        return "先后、竞争或过量不足"
    return {
        "无反应关系": "无反应关系",
        "单一反应": "单一直接反应",
        "单一直接反应": "单一直接反应",
        "2-3个并列或简单连续反应": "2-3个并列或简单连续反应",
        "多反应连续转化": "多反应连续转化",
    }.get(reaction, reaction)


def _map_legacy_evidence(features: Mapping[str, Any]) -> str:
    evidence = _text(features.get("evidence_relation"))
    interference = _text(features.get("interference_exclusion"))
    if evidence in {"证据冲突与筛选", "证据冲突、筛选或多层排除"} or interference in {"多个干扰", "多层证据冲突"}:
        return "证据冲突、筛选或多层排除"
    if evidence in {"干扰排除", "需要排除竞争解释"} or interference == "单一干扰":
        return "需要排除竞争解释"
    return {
        "无证据链": "无证据任务",
        "无证据任务": "无证据任务",
        "单一现象对应": "单一证据直接对应",
        "单一证据直接对应": "单一证据直接对应",
        "多条清晰证据链": "多条清晰证据联合",
        "多条清晰证据联合": "多条清晰证据联合",
    }.get(evidence, evidence)


def convert_legacy_features(features: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Convert a historical V5.2 feature object; never called implicitly."""
    core = {
        "reasoning_depth": _text(features.get("reasoning_depth")),
        "reasoning_direction": _text(features.get("reasoning_direction")),
        "knowledge_relation": _text(features.get("knowledge_relation")),
        "representation_conversion": {
            "两类表征往返": "两类表征连续转换",
        }.get(_text(features.get("representation_conversion")), _text(features.get("representation_conversion"))),
        "reaction_relation": _map_legacy_reaction(features),
        "constraint_complexity": _text(features.get("constraint_complexity", features.get("constraint_count"))),
        "evidence_relation": _map_legacy_evidence(features),
        "experiment_requirement": {
            "控制变量或现象解释": "控制变量、现象解释或数据归纳",
            "方案设计或评价": "方案设计、评价或补充实验",
        }.get(_text(features.get("experiment_requirement")), _text(features.get("experiment_requirement"))),
        "graph_table_requirement": {
            "拐点或分段反推": "拐点、平台或分段反推",
        }.get(_text(features.get("graph_table_requirement")), _text(features.get("graph_table_requirement"))),
        "calculation_model": {
            "多重守恒差量联立或分类": "多重守恒、差量、联立或分类",
        }.get(_text(features.get("calculation_model")), _text(features.get("calculation_model"))),
        "unfamiliar_information_transfer": {
            "无": "课内直接原型",
            "课内原型": "课内直接原型",
            "给定信息直接套用": "给定新信息直接应用",
            "迁移后推导": "迁移后建立关系",
        }.get(_text(features.get("unfamiliar_information_transfer")), _text(features.get("unfamiliar_information_transfer"))),
        "subquestion_dependency": {
            "多问但相互独立": "多问相互独立",
            "多问且存在前后依赖": "多问存在结果或任务链依赖",
            "多问且层层递进": "多问存在结果或任务链依赖",
        }.get(_text(features.get("subquestion_dependency")), _text(features.get("subquestion_dependency"))),
    }
    audit = {field: _text(features.get(field)) for field in AUDIT_FEATURE_FIELDS}
    return validate_core_features(core), validate_audit_features(audit)


def validate_and_prepare_result(
    result: Mapping[str, Any],
    data: Mapping[str, Any],
    *,
    allow_legacy: bool = False,
) -> dict[str, Any]:
    """Validate the compact four-field production JSON.

    Question/subquestion reconstruction remains an internal reasoning step.  It
    is intentionally absent from the production contract, so image-recognised
    compound questions cannot be rejected merely because the source JSONL did
    not separately expose ``sub_questions``.
    """
    if not isinstance(result, dict):
        raise Core12SchemaError("模型输出必须是JSON对象")
    prepared = copy.deepcopy(result)
    required_top = {"features", "coarse_difficulty", "reasoning", "difficulty_level"}
    missing_top = sorted(required_top - set(prepared))
    extra_top = sorted(set(prepared) - required_top)
    if missing_top or extra_top:
        raise Core12SchemaError(
            f"生产JSON顶层字段不固定：missing={missing_top}, extra={extra_top}"
        )
    forbidden = sorted(BANNED_PRODUCTION_FIELDS.intersection(prepared))
    if forbidden:
        raise Core12SchemaError(f"生产输出包含已删除字段: {forbidden}")

    raw_features = prepared["features"]
    if allow_legacy and isinstance(raw_features, dict):
        missing_evidence = {"entry_operation", "task_rule_breadth", "visual_information_role"} - set(raw_features)
        if missing_evidence == {"entry_operation", "task_rule_breadth", "visual_information_role"}:
            raw_features = dict(raw_features)
            depth = _text(raw_features.get("reasoning_depth"))
            raw_features["entry_operation"] = {
                "0层": "直接检索",
                "1层": "自主选择一条规则",
                "2-3层": "形成中间结论后应用",
                "4-5层": "多节点连续决策",
                "6层及以上": "多节点连续决策",
            }.get(depth, "多节点连续决策")
            dependency = _text(raw_features.get("subquestion_dependency"))
            raw_features["task_rule_breadth"] = {
                "无多问": "单一规则或同类检索束",
                "多问相互独立": "多个独立回答规则",
                "多问共享模型但无答案依赖": "共享模型但无结果依赖",
                "多问存在结果或任务链依赖": "存在结果或任务链依赖",
            }.get(dependency, "单一规则或同类检索束")
            representation = _text(raw_features.get("representation_conversion"))
            graph = _text(raw_features.get("graph_table_requirement"))
            raw_features["visual_information_role"] = (
                "决定模型、阶段或任务链" if graph in {"拐点、平台或分段反推", "多图表耦合建模"}
                else "提供局部关系" if representation != "无" or graph != "无"
                else "无实质视觉信息"
            )
    core = validate_features(raw_features)
    level = _text(prepared.get("difficulty_level"))
    if level not in LEVELS:
        raise Core12SchemaError(f"difficulty_level非法: {level!r}")
    coarse = _text(prepared.get("coarse_difficulty"))
    if coarse not in COARSE_LEVELS:
        raise Core12SchemaError(f"coarse_difficulty非法: {coarse!r}")
    if coarse not in ALLOWED_COARSE_BY_LEVEL[level]:
        raise Core12SchemaError(
            "coarse_difficulty与difficulty_level不在同一相邻边界："
            f"level={level!r}, coarse={coarse!r}, "
            f"allowed={sorted(ALLOWED_COARSE_BY_LEVEL[level])}"
        )
    reasoning = prepared.get("reasoning")
    reasoning_fields = {"core_basis", "hard_point", "why_not_lower", "why_not_higher"}
    if not isinstance(reasoning, dict) or set(reasoning) != reasoning_fields:
        raise Core12SchemaError("reasoning必须且只能包含4个固定字段")
    if any(not _text(reasoning[field]) for field in reasoning_fields):
        raise Core12SchemaError("reasoning的4个字段均不得为空")

    prepared["features"] = core
    prepared["feature_schema_version"] = "chemistry_evidence15_v3"
    prepared["schema_validation_passed"] = True
    prepared["automatic_level_change_applied"] = False
    prepared["postprocess_original_level"] = level
    prepared["postprocess_trace"] = []
    prepared["postprocess_actions"] = []
    prepared["postprocess_profile"] = "core12_schema_only"
    prepared["feature_audit_flags"] = []
    return prepared


def apply_safe_adjacent_level_change(
    prepared: Mapping[str, Any],
    new_level: str,
    reason: str,
) -> dict[str, Any]:
    """Apply one already-approved Core-12 rule while enforcing safety limits.

    This guard does not decide when a rule should fire. It prevents a future
    offline-approved rule from changing more than one adjacent level or using
    audit features as its evidence source.
    """
    result = copy.deepcopy(dict(prepared))
    old_level = _text(result.get("difficulty_level"))
    if old_level not in LEVELS or new_level not in LEVELS:
        raise Core12SchemaError(f"safe-rules档位非法: {old_level!r} -> {new_level!r}")
    if abs(LEVELS.index(old_level) - LEVELS.index(new_level)) != 1:
        raise Core12SchemaError(
            f"safe-rules每题最多调整一个相邻档: {old_level} -> {new_level}"
        )
    if not _text(reason):
        raise Core12SchemaError("safe-rules改档必须记录明确原因")
    if "audit_features" in reason:
        raise Core12SchemaError("safe-rules不得读取audit_features改档")
    result["difficulty_level"] = new_level
    result["coarse_difficulty"] = PAIR_COARSE[frozenset({old_level, new_level})]
    result["automatic_level_change_applied"] = True
    result["postprocess_profile"] = "core12_safe_rules"
    result["postprocess_actions"] = [{
        "from": old_level,
        "to": new_level,
        "reason": reason,
    }]
    result["postprocess_trace"] = copy.deepcopy(result["postprocess_actions"])
    return result


def _flatten_runtime_text(data: Mapping[str, Any], prepared: Mapping[str, Any]) -> tuple[str, str]:
    """Return task-facing text and solution/evidence text without IDs or labels."""

    task_parts: list[str] = []
    for field in ("stem", "options", "question", "content", "image_text_supplement"):
        value = data.get(field)
        if value:
            task_parts.append(str(value))

    sub_questions = data.get("sub_questions")
    if isinstance(sub_questions, list):
        for item in sub_questions:
            if isinstance(item, dict):
                for field in ("stem", "question", "content", "options"):
                    if item.get(field):
                        task_parts.append(str(item[field]))
            elif item:
                task_parts.append(str(item))

    solution_parts = [str(data.get("analysis") or "")]
    reasoning = prepared.get("reasoning")
    if isinstance(reasoning, Mapping):
        solution_parts.extend([
            str(reasoning.get("core_basis") or ""),
            str(reasoning.get("hard_point") or ""),
        ])
    return "\n".join(task_parts), "\n".join(solution_parts)


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _task_block_count(text: str) -> int:
    markers = set(re.findall(r"(?:任务|活动|探究)[一二三四五六七八九十]", text))
    numbered = set(re.findall(r"(?:^|[\n；;])\s*[（(]?([1-9]\d*)[）).、]", text))
    return max(len(markers), len(numbered))


def _data_aware_semantics(task_text: str, solution_text: str) -> dict[str, bool]:
    full_text = f"{task_text}\n{solution_text}"
    return {
        "explicit_application": _contains_any(
            task_text,
            ("计算", "求出", "解释原因", "说明理由", "写出化学方程式", "设计实验", "补充实验", "评价方案", "判断并说明"),
        ),
        "routine_experiment": _contains_any(
            full_text,
            ("实验", "装置", "操作", "现象", "控制变量", "气密性", "传感器", "测定"),
        ),
        "design_or_evaluation": _contains_any(
            full_text,
            ("设计实验", "设计方案", "补充实验", "评价方案", "方案是否可行", "改进方案", "证明", "验证", "证据充分", "排除干扰"),
        ),
        "interference_or_exclusion": _contains_any(
            full_text,
            ("干扰", "排除", "不能证明", "不足以说明", "唯一", "过量试剂", "上层清液", "反例", "异常"),
        ),
        "reaction_sequence": _contains_any(
            full_text,
            ("过量", "少量", "逐滴", "先后", "继续加入", "充分反应后", "反应后", "剩余", "滤液", "滤渣", "上层清液", "部分反应"),
        ),
        "process_or_composition": _contains_any(
            full_text,
            ("工艺流程", "流程图", "回收", "制备", "除杂", "分离", "转化", "溶质组成", "成分判断", "物质去向"),
        ),
        "graph_stage": _contains_any(
            full_text,
            ("曲线", "图像", "图象", "拐点", "平台", "阶段", "pH变化", "变化趋势", "a点", "b点", "c点", "d点"),
        ),
        "quantitative_chain": _contains_any(
            full_text,
            ("质量分数", "纯度", "含量", "质量守恒", "元素守恒", "差量", "联立", "计算", "质量比", "产率", "范围"),
        ),
        "new_model": _contains_any(
            full_text,
            ("查阅资料", "给出资料", "陌生", "项目式", "自主招生", "竞赛", "新型装置", "新信息"),
        ),
        "dependency_language": _contains_any(
            full_text,
            ("根据上述", "由此", "所得", "再向其中", "取上层清液", "继续", "进一步探究", "再探", "用于后续", "由前一问"),
        ),
    }


def _core12_signal_counts(features: Mapping[str, str]) -> dict[str, int]:
    medium = sum([
        features["entry_operation"] in {"形成中间结论后应用", "多节点连续决策"},
        features["task_rule_breadth"] in {"共享模型但无结果依赖", "存在结果或任务链依赖"},
        features["visual_information_role"] in {"提供局部关系", "决定模型、阶段或任务链"},
        features["reasoning_depth"] in {"2-3层", "4-5层", "6层及以上"},
        features["knowledge_relation"] in {"同模块深度关联", "跨模块融合", "多模块深度融合"},
        features["representation_conversion"] in {"两类表征连续转换", "宏观-微观-符号-定量多重转换"},
        features["reaction_relation"] in {"2-3个并列或简单连续反应", "多反应连续转化", "先后、竞争或过量不足", "需要分情况判断的反应模型"},
        features["evidence_relation"] in {"多条清晰证据联合", "需要排除竞争解释", "证据冲突、筛选或多层排除"},
        features["experiment_requirement"] in {"控制变量、现象解释或数据归纳", "方案设计、评价或补充实验", "多阶段探究与定量误差"},
        features["graph_table_requirement"] in {"多组比较归纳", "拐点、平台或分段反推", "多图表耦合建模"},
        features["calculation_model"] in {"单一方程式或关系式", "单一守恒或多反应计算", "多重守恒、差量、联立或分类"},
        features["subquestion_dependency"] in {"多问共享模型但无答案依赖", "多问存在结果或任务链依赖"},
    ])
    high = sum([
        features["entry_operation"] == "多节点连续决策",
        features["task_rule_breadth"] == "存在结果或任务链依赖",
        features["visual_information_role"] == "决定模型、阶段或任务链",
        features["reasoning_depth"] in {"4-5层", "6层及以上"},
        features["reasoning_direction"] in {"逆向推导", "分类讨论或综合推导"},
        features["constraint_complexity"] in {"多个相互关联约束", "多层嵌套约束"},
        features["reaction_relation"] in {"多反应连续转化", "先后、竞争或过量不足", "需要分情况判断的反应模型"},
        features["evidence_relation"] in {"需要排除竞争解释", "证据冲突、筛选或多层排除"},
        features["experiment_requirement"] in {"方案设计、评价或补充实验", "多阶段探究与定量误差"},
        features["graph_table_requirement"] in {"拐点、平台或分段反推", "多图表耦合建模"},
        features["calculation_model"] in {"单一守恒或多反应计算", "多重守恒、差量、联立或分类"},
        features["unfamiliar_information_transfer"] in {"迁移后建立关系", "完全陌生模型现场建立"},
    ])
    final = sum([
        features["entry_operation"] == "多节点连续决策",
        features["task_rule_breadth"] == "存在结果或任务链依赖",
        features["visual_information_role"] == "决定模型、阶段或任务链",
        features["reasoning_depth"] == "6层及以上",
        features["knowledge_relation"] == "多模块深度融合",
        features["representation_conversion"] == "宏观-微观-符号-定量多重转换",
        features["constraint_complexity"] == "多层嵌套约束",
        features["evidence_relation"] == "证据冲突、筛选或多层排除",
        features["experiment_requirement"] == "多阶段探究与定量误差",
        features["graph_table_requirement"] == "多图表耦合建模",
        features["calculation_model"] == "多重守恒、差量、联立或分类",
        features["unfamiliar_information_transfer"] == "完全陌生模型现场建立",
        features["subquestion_dependency"] == "多问存在结果或任务链依赖",
    ])
    return {"medium": medium, "high": high, "final": final}


def _apply_data_aware_change(
    result: dict[str, Any],
    new_level: str,
    rule: str,
    reason: str,
    structural_evidence: list[str],
    semantic_evidence: list[str],
    protections: list[str],
    counts: Mapping[str, int],
) -> dict[str, Any]:
    old_level = _text(result["difficulty_level"])
    if abs(LEVELS.index(old_level) - LEVELS.index(new_level)) != 1:
        raise Core12SchemaError(f"数据感知后处理只允许相邻档调整: {old_level}->{new_level}")
    result["difficulty_level"] = new_level
    result["coarse_difficulty"] = PAIR_COARSE[frozenset({old_level, new_level})]
    result["automatic_level_change_applied"] = True
    result["postprocess_profile"] = "evidence15_boundary_rules_v6"
    action = {
        "from": old_level,
        "to": new_level,
        "rule": rule,
        "reason": reason,
        "structural_evidence": structural_evidence,
        "semantic_evidence": semantic_evidence,
        "protections_checked": protections,
    }
    result["postprocess_actions"] = [action]
    result["postprocess_trace"] = copy.deepcopy(result["postprocess_actions"])
    result["postprocess_evidence_counts"] = dict(counts)
    _append_reasoning_calibration(result, old_level=old_level, new_level=new_level, reason=reason)
    return result


def apply_data_aware_boundary_rules(
    prepared: Mapping[str, Any],
    data: Mapping[str, Any],
) -> dict[str, Any]:
    """Chemistry boundary calibration using features, source semantics and guards.

    The function never reads labels, question identifiers or curated bad-case
    lists.  Missing high-order features are not treated as downgrade evidence.
    Every change needs positive semantic and structural support and remains
    limited to one adjacent level.
    """

    result = copy.deepcopy(dict(prepared))
    features = result["features"]
    level = _text(result["difficulty_level"])
    entry = features["entry_operation"]
    breadth = features["task_rule_breadth"]
    visual_role = features["visual_information_role"]
    task_text, solution_text = _flatten_runtime_text(data, result)
    semantics = _data_aware_semantics(task_text, solution_text)
    counts = _core12_signal_counts(features)
    blocks = _task_block_count(task_text)
    semantic_high = [
        name for name in (
            "design_or_evaluation", "interference_or_exclusion", "reaction_sequence",
            "process_or_composition", "graph_stage", "quantitative_chain", "new_model",
            "dependency_language",
        ) if semantics[name]
    ]
    protections: list[str] = []

    if features["subquestion_dependency"] in {"无多问", "多问相互独立"}:
        protections.append("无任务链依赖")
    if not semantic_high:
        protections.append("题面未发现高阶语义")
    if blocks <= 1:
        protections.append("单任务块")

    change: tuple[str, str, str, list[str], list[str]] | None = None

    if level == "送分题":
        structural = []
        if features["representation_conversion"] != "无":
            structural.append("存在实质表征转换")
        if features["reaction_relation"] != "无反应关系":
            structural.append("需要应用反应关系")
        if features["experiment_requirement"] != "无":
            structural.append("需要实验操作或分析")
        if features["calculation_model"] != "无":
            structural.append("需要计算关系")
        semantic = []
        if entry == "自主选择一条规则":
            semantic.append("解题入口需要自主选择规则")
        if entry == "自主选择一条规则" and breadth == "多个独立回答规则":
            semantic.append("整题包含多个不同回答规则")
        if entry in {"形成中间结论后应用", "多节点连续决策"}:
            semantic.append("题面要求生成并继续使用中间结论")
        if semantic and (
            structural
            or (entry == "自主选择一条规则" and breadth == "多个独立回答规则")
        ):
            change = (
                "基础题", "dataaware_easy_to_basic",
                "题面存在明确的规律应用动作，且Core-12至少记录一项实质转换，不能停在直接识别档。",
                structural, semantic,
            )

    elif level == "基础题":
        direct_bundle = (
            entry in {"直接检索", "一次透明映射"}
            and breadth == "单一规则或同类检索束"
            and visual_role in {"无实质视觉信息", "仅呈现或重复文字", "直接识图作答"}
            and features["representation_conversion"] in {"无", "一次表征转换"}
            and features["reaction_relation"] in {"无反应关系", "单一直接反应"}
            and features["constraint_complexity"] in {"无约束", "单一约束"}
            and features["evidence_relation"] in {"无证据任务", "单一证据直接对应"}
            and features["experiment_requirement"] in {"无", "基础操作或读数"}
            and features["graph_table_requirement"] in {"无", "直接读数"}
            and features["calculation_model"] in {"无", "口算或直接比例"}
            and features["subquestion_dependency"] in {"无多问", "多问相互独立"}
        )
        if direct_bundle:
            change = (
                "送分题", "evidence15_basic_to_easy",
                "最高难任务只是直接检索或一次透明映射；多个空仍使用同一回答规则，图片只承担呈现或直接识别。",
                [entry, breadth, visual_role],
                ["无自主规律选择、无中间结论、无连续任务边"],
            )
        structural = []
        semantic = []
        experiment_chain = (
            semantics["routine_experiment"]
            and features["experiment_requirement"] in {"控制变量、现象解释或数据归纳", "方案设计、评价或补充实验", "多阶段探究与定量误差"}
            and (features["evidence_relation"] in {"多条清晰证据联合", "需要排除竞争解释", "证据冲突、筛选或多层排除"} or semantics["graph_stage"])
        )
        calculation_chain = (
            semantics["quantitative_chain"]
            and features["calculation_model"] in {"单一方程式或关系式", "单一守恒或多反应计算", "多重守恒、差量、联立或分类"}
            and (semantics["reaction_sequence"] or semantics["graph_stage"] or features["reasoning_depth"] == "2-3层")
        )
        shared_chain = (
            blocks >= 3
            and entry in {"形成中间结论后应用", "多节点连续决策"}
            and breadth == "存在结果或任务链依赖"
            and features["subquestion_dependency"] == "多问存在结果或任务链依赖"
            and counts["medium"] >= 3
        )
        if experiment_chain:
            structural.extend(["完整实验流程", "联合证据"])
            semantic.append("操作—现象/数据—结论需要连续处理")
        if calculation_chain:
            structural.extend(["完整计算模型", "反应阶段或图像条件"])
            semantic.append("定量关系依赖前置反应/阶段判断")
        if shared_chain:
            structural.extend(["至少三个任务块", "共享模型或任务依赖"])
            semantic.append("多个任务不能还原为独立一步核验")
        if change is None and (experiment_chain or calculation_chain or shared_chain or entry in {"形成中间结论后应用", "多节点连续决策"}):
            if entry in {"形成中间结论后应用", "多节点连续决策"}:
                structural.append(entry)
                semantic.append("前置结论继续参与后续判断")
            change = (
                "中等题", "dataaware_basic_to_medium",
                "题面与Core-12共同证明存在完整常规模型或连续依赖，超过一步应用。",
                list(dict.fromkeys(structural)), list(dict.fromkeys(semantic)),
            )

    elif level == "中等题":
        low_profile = (
            features["reasoning_depth"] in {"0层", "1层"}
            and features["knowledge_relation"] in {"单一知识点", "同模块简单关联"}
            and features["representation_conversion"] in {"无", "一次表征转换"}
            and features["experiment_requirement"] in {"无", "基础操作或读数"}
            and features["graph_table_requirement"] in {"无", "直接读数"}
            and features["calculation_model"] in {"无", "口算或直接比例"}
            and features["subquestion_dependency"] in {"无多问", "多问相互独立"}
            and entry in {"直接检索", "一次透明映射", "自主选择一条规则"}
            and breadth in {"单一规则或同类检索束", "多个独立回答规则"}
            and visual_role != "决定模型、阶段或任务链"
            and not semantic_high
            and blocks <= 1
        )
        if low_profile:
            change = (
                "基础题", "dataaware_medium_to_basic",
                "题面是单任务显性应用，且低结构特征形成完整正证据；并非仅因高阶特征缺失而降档。",
                ["0-1层", "无完整实验/计算模型", "无共享任务链"],
                ["单任务块且无高阶语义"],
            )
        else:
            decisive: list[str] = []
            semantic: list[str] = []
            if (
                semantics["design_or_evaluation"]
                and semantics["interference_or_exclusion"]
                and features["experiment_requirement"] in {"方案设计、评价或补充实验", "多阶段探究与定量误差"}
                and features["evidence_relation"] in {"需要排除竞争解释", "证据冲突、筛选或多层排除"}
                and features["constraint_complexity"] in {"多个相互关联约束", "多层嵌套约束"}
            ):
                decisive.append("方案充分性/干扰排除")
                semantic.append("实验结论依赖排除竞争解释")
            if (
                semantics["reaction_sequence"]
                and semantics["process_or_composition"]
                and features["reaction_relation"] in {"多反应连续转化", "先后、竞争或过量不足", "需要分情况判断的反应模型"}
                and features["constraint_complexity"] in {"多个相互关联约束", "多层嵌套约束"}
                and features["subquestion_dependency"] == "多问存在结果或任务链依赖"
            ):
                decisive.append("反应先后/过量改变流程成分")
                semantic.append("前一节点决定后续物质或检验路径")
            if (
                semantics["graph_stage"]
                and semantics["quantitative_chain"]
                and features["graph_table_requirement"] in {"拐点、平台或分段反推", "多图表耦合建模"}
                and features["calculation_model"] in {"单一方程式或关系式", "单一守恒或多反应计算", "多重守恒、差量、联立或分类"}
                and features["representation_conversion"] in {"两类表征连续转换", "宏观-微观-符号-定量多重转换"}
                and features["constraint_complexity"] in {"多个相互关联约束", "多层嵌套约束"}
            ):
                decisive.append("图像阶段决定定量模型")
                semantic.append("必须先判定数据段再建立计算关系")
            if (
                semantics["quantitative_chain"]
                and semantics["reaction_sequence"]
                and semantics["process_or_composition"]
                and features["calculation_model"] in {"单一守恒或多反应计算", "多重守恒、差量、联立或分类"}
                and features["subquestion_dependency"] == "多问存在结果或任务链依赖"
            ):
                decisive.append("反应阶段与守恒模型耦合")
                semantic.append("组分/过量判断改变守恒或方程")
            if visual_role == "决定模型、阶段或任务链" and entry == "多节点连续决策":
                decisive.append("图片关系决定阶段、模型或后续任务")
                semantic.append("视觉信息不是版式，而是解题链中的决定性节点")
            if decisive and (counts["high"] >= 2 or len(decisive) >= 2):
                change = (
                    "拔高题", "dataaware_medium_to_hard",
                    "至少一个决定性化学卡点获得题面语义与结构特征交叉支持。",
                    decisive, semantic,
                )

    elif level == "拔高题":
        # A downgrade requires an independently proven routine structure.
        # Failure to meet a promotion threshold is never a downgrade reason.
        positive_routine = (
            features["reasoning_depth"] == "2-3层"
            and features["reasoning_direction"] == "正向推导"
            and features["constraint_complexity"] in {"无约束", "单一约束"}
            and features["evidence_relation"] in {"无证据任务", "单一证据直接对应", "多条清晰证据联合"}
            and features["experiment_requirement"] in {"无", "基础操作或读数", "控制变量、现象解释或数据归纳"}
            and features["calculation_model"] in {"无", "口算或直接比例", "单一方程式或关系式"}
            and features["subquestion_dependency"] in {"无多问", "多问相互独立"}
            and not semantics["design_or_evaluation"]
            and not semantics["interference_or_exclusion"]
            and not semantics["reaction_sequence"]
            and not semantics["process_or_composition"]
            and not (semantics["graph_stage"] and semantics["quantitative_chain"])
            and blocks <= 1
        )
        holistic_categories = sum([
            semantics["design_or_evaluation"], semantics["reaction_sequence"],
            semantics["process_or_composition"], semantics["graph_stage"],
            semantics["quantitative_chain"], semantics["new_model"],
        ])
        # A final-level promotion requires the model to have explicitly
        # reconstructed a task/result dependency.  Surface transition words
        # are useful audit evidence but cannot replace this structural gate.
        dependency_support = (
            features["subquestion_dependency"] == "多问存在结果或任务链依赖"
            and breadth == "存在结果或任务链依赖"
        )
        decisive_final_support = (
            features["evidence_relation"] in {"需要排除竞争解释", "证据冲突、筛选或多层排除"}
            or features["experiment_requirement"] == "多阶段探究与定量误差"
            or (
                semantics["design_or_evaluation"]
                and semantics["new_model"]
                and semantics["reaction_sequence"]
                and dependency_support
            )
        )
        full_chemistry_chain = (
            features["knowledge_relation"] in {"跨模块融合", "多模块深度融合"}
            and features["representation_conversion"] == "宏观-微观-符号-定量多重转换"
            and features["reaction_relation"] in {"多反应连续转化", "先后、竞争或过量不足", "需要分情况判断的反应模型"}
            and features["calculation_model"] in {"单一守恒或多反应计算", "多重守恒、差量、联立或分类"}
        )
        holistic_chain = (
            dependency_support
            and full_chemistry_chain
            and decisive_final_support
            and counts["high"] >= 4
            and (
                holistic_categories >= 4
                or (
                    semantics["design_or_evaluation"]
                    and semantics["new_model"]
                    and semantics["reaction_sequence"]
                    and semantics["quantitative_chain"]
                )
            )
        )
        if holistic_chain:
            structural = [
                "多任务存在结果/条件依赖",
                f"高阶结构证据{counts['high']}项",
                f"压轴强证据{counts['final']}项",
            ]
            semantic = [name for name in semantic_high if name in {
                "design_or_evaluation", "reaction_sequence", "process_or_composition",
                "graph_stage", "quantitative_chain", "new_model", "dependency_language",
            }]
            change = (
                "压轴题", "dataaware_hard_to_final",
                "多个高阶任务通过物质、条件、数据段或中间结论耦合，形成不可拆分的整体任务链。",
                structural, semantic,
            )
        elif positive_routine:
            change = (
                "中等题", "dataaware_hard_to_medium",
                "题面和特征共同证明只有单任务常规正向链，存在独立的常规结构正证据。",
                ["2-3层正向推导", "单一约束", "无任务链依赖"],
                ["无方案排他性、反应阶段、流程去向或分段定量卡点"],
            )

    # Raw final-level predictions are protected: absence of extracted evidence
    # cannot prove that a pressure-test problem should be downgraded.
    if change is None:
        result["postprocess_profile"] = "evidence15_boundary_rules_v6"
        result["automatic_level_change_applied"] = False
        result["postprocess_evidence_counts"] = dict(counts)
        result["postprocess_semantic_flags"] = semantics
        result["postprocess_task_block_count"] = blocks
        result["postprocess_protections"] = protections
        return result

    new_level, rule, reason, structural, semantic = change
    result["postprocess_semantic_flags"] = semantics
    result["postprocess_task_block_count"] = blocks
    result["postprocess_protections"] = protections
    return _apply_data_aware_change(
        result,
        new_level,
        rule,
        reason,
        structural,
        semantic,
        protections,
        counts,
    )


PAIR_COARSE = {
    frozenset({"送分题", "基础题"}): "送分/基础区间（1-2档）",
    frozenset({"基础题", "中等题"}): "基础/中等区间（2-3档）",
    frozenset({"中等题", "拔高题"}): "中等/拔高区间（3-4档）",
    frozenset({"拔高题", "压轴题"}): "拔高/压轴区间（4-5档）",
}


def _append_reasoning_calibration(
    result: dict[str, Any],
    *,
    old_level: str,
    new_level: str,
    reason: str,
) -> None:
    reasoning = result["reasoning"]
    reasoning["core_basis"] = (
        f"{_text(reasoning['core_basis'])} "
        f"后处理仅做Core-12证据一致性校准：{reason}"
    ).strip()
    adjacent_text = {
        ("基础题", "送分题"): (
            "最终任务没有自主规律选择或中间结论，符合直接识别边界。",
            "若存在一次实质转换或不同回答规则切换，才需要基础题。",
        ),
        ("送分题", "基础题"): (
            "已经出现一次实质转换或自主规则选择，不能停在直接识别。",
            "尚无2—3层连续依赖、共享模型或完整常规模型。",
        ),
        ("中等题", "基础题"): (
            "证据只支持显性一步应用或相互独立核验。",
            "没有2—3层连续依赖、共享中间结论或完整常规模型。",
        ),
        ("基础题", "中等题"): (
            "存在2—3层连续依赖、联合证据或完整常规模型，超过一步应用。",
            "仍无决定性逆向卡点、竞争解释、分段反推或多个关联约束。",
        ),
        ("拔高题", "中等题"): (
            "任务可由常规正向链解决，高阶信号没有形成共同卡点。",
            "没有至少两个高阶证据共同支持决定性卡点。",
        ),
        ("中等题", "拔高题"): (
            "至少两个Core-12高阶维度共同指向会改变后续路径的卡点。",
            "尚未形成多个高阶任务互相改变模型的完整压轴链。",
        ),
        ("拔高题", "压轴题"): (
            "多项强证据共同形成不可拆分的整体任务链，超过单一高阶卡点。",
            "压轴题已经是最高档。",
        ),
    }.get((old_level, new_level))
    if adjacent_text:
        reasoning["why_not_lower"], reasoning["why_not_higher"] = adjacent_text


def apply_targeted_evidence_rules(prepared: Mapping[str, Any]) -> dict[str, Any]:
    """Apply conservative, auditable one-boundary evidence-consistency rules.

    The rules only reconcile ``difficulty_level`` with the model's own strict
    Core-12 enums.  They never inspect teacher labels, question IDs, keywords
    from the source dataset, or the optional audit fields.
    """
    result = copy.deepcopy(dict(prepared))
    features = result["features"]
    level = _text(result["difficulty_level"])

    medium_signals = {
        "depth": features["reasoning_depth"] in {"2-3层", "4-5层", "6层及以上"},
        "knowledge": features["knowledge_relation"] in {"同模块深度关联", "跨模块融合", "多模块深度融合"},
        "representation": features["representation_conversion"] in {"两类表征连续转换", "宏观-微观-符号-定量多重转换"},
        "reaction": features["reaction_relation"] in {"2-3个并列或简单连续反应", "多反应连续转化", "先后、竞争或过量不足", "需要分情况判断的反应模型"},
        "evidence": features["evidence_relation"] in {"多条清晰证据联合", "需要排除竞争解释", "证据冲突、筛选或多层排除"},
        "experiment": features["experiment_requirement"] in {"控制变量、现象解释或数据归纳", "方案设计、评价或补充实验", "多阶段探究与定量误差"},
        "graph": features["graph_table_requirement"] in {"多组比较归纳", "拐点、平台或分段反推", "多图表耦合建模"},
        "calculation": features["calculation_model"] in {"单一方程式或关系式", "单一守恒或多反应计算", "多重守恒、差量、联立或分类"},
        "dependency": features["subquestion_dependency"] in {"多问共享模型但无答案依赖", "多问存在结果或任务链依赖"},
    }
    high_signals = {
        "depth": features["reasoning_depth"] in {"4-5层", "6层及以上"},
        "direction": features["reasoning_direction"] in {"逆向推导", "分类讨论或综合推导"},
        "constraint": features["constraint_complexity"] in {"多个相互关联约束", "多层嵌套约束"},
        "reaction": features["reaction_relation"] in {"多反应连续转化", "先后、竞争或过量不足", "需要分情况判断的反应模型"},
        "evidence": features["evidence_relation"] in {"需要排除竞争解释", "证据冲突、筛选或多层排除"},
        "experiment": features["experiment_requirement"] in {"方案设计、评价或补充实验", "多阶段探究与定量误差"},
        "graph": features["graph_table_requirement"] in {"拐点、平台或分段反推", "多图表耦合建模"},
        "calculation": features["calculation_model"] in {"单一守恒或多反应计算", "多重守恒、差量、联立或分类"},
        "transfer": features["unfamiliar_information_transfer"] in {"迁移后建立关系", "完全陌生模型现场建立"},
    }
    final_signals = {
        "depth": features["reasoning_depth"] == "6层及以上",
        "knowledge": features["knowledge_relation"] == "多模块深度融合",
        "representation": features["representation_conversion"] == "宏观-微观-符号-定量多重转换",
        "constraint": features["constraint_complexity"] == "多层嵌套约束",
        "evidence": features["evidence_relation"] == "证据冲突、筛选或多层排除",
        "experiment": features["experiment_requirement"] == "多阶段探究与定量误差",
        "graph": features["graph_table_requirement"] == "多图表耦合建模",
        "calculation": features["calculation_model"] == "多重守恒、差量、联立或分类",
        "transfer": features["unfamiliar_information_transfer"] == "完全陌生模型现场建立",
        "dependency": features["subquestion_dependency"] == "多问存在结果或任务链依赖",
    }
    medium_count = sum(medium_signals.values())
    high_count = sum(high_signals.values())
    final_count = sum(final_signals.values())

    change: tuple[str, str] | None = None
    if level == "基础题":
        direct_profile = (
            features["reasoning_depth"] == "0层"
            and features["reasoning_direction"] == "直接识记"
            and features["knowledge_relation"] == "单一知识点"
            and features["representation_conversion"] == "无"
            and features["reaction_relation"] == "无反应关系"
            and features["constraint_complexity"] == "无约束"
            and features["evidence_relation"] == "无证据任务"
            and features["experiment_requirement"] == "无"
            and features["graph_table_requirement"] == "无"
            and features["calculation_model"] == "无"
            and features["unfamiliar_information_transfer"] == "课内直接原型"
            and features["subquestion_dependency"] == "无多问"
        )
        if direct_profile:
            change = ("送分题", "全部核心特征均为直接识别且无规则切换，修正典型1→2高估。")
        elif medium_count >= 3 and features["reasoning_depth"] == "2-3层":
            change = ("中等题", f"中等结构证据达到{medium_count}项且存在2—3层连续依赖，修正典型3→2压档。")
    elif level == "送分题":
        if features["reasoning_depth"] == "1层" or features["representation_conversion"] != "无":
            change = ("基础题", "存在1层实质应用或表征转换，与直接识别档不一致。")
    elif level == "中等题":
        if (
            high_count >= 3
            and high_signals["depth"]
            and any(high_signals[key] for key in ("constraint", "evidence", "experiment", "graph", "calculation"))
        ):
            change = ("拔高题", f"高阶证据达到{high_count}项并包含决定性任务卡点，修正典型4→3压档。")
        elif medium_count <= 1 and features["reasoning_depth"] in {"0层", "1层"}:
            change = ("基础题", "仅有0—1层显性应用且没有共享常规模型，修正常规任务被过度聚合。")
    elif level == "拔高题":
        complete_chain_gate = (
            final_count >= 4
            and final_signals["depth"]
            and final_signals["dependency"]
            and any(final_signals[key] for key in ("constraint", "experiment", "calculation", "transfer"))
        )
        if complete_chain_gate:
            change = ("压轴题", f"压轴强证据达到{final_count}项且存在任务链依赖，修正典型5→4压档。")
        elif high_count <= 1 and features["reasoning_depth"] == "2-3层":
            change = ("中等题", "只有常规正向结构，没有形成决定性高阶卡点。")

    if not change:
        result["postprocess_profile"] = "core12_targeted_rules_v3"
        result["automatic_level_change_applied"] = False
        result["postprocess_evidence_counts"] = {
            "medium": medium_count,
            "high": high_count,
            "final": final_count,
        }
        return result

    new_level, reason = change
    old_level = level
    result["difficulty_level"] = new_level
    result["coarse_difficulty"] = PAIR_COARSE[frozenset({old_level, new_level})]
    result["automatic_level_change_applied"] = True
    result["postprocess_profile"] = "core12_targeted_rules_v3"
    result["postprocess_actions"] = [{"from": old_level, "to": new_level, "reason": reason}]
    result["postprocess_trace"] = copy.deepcopy(result["postprocess_actions"])
    result["postprocess_evidence_counts"] = {
        "medium": medium_count,
        "high": high_count,
        "final": final_count,
    }
    _append_reasoning_calibration(
        result,
        old_level=old_level,
        new_level=new_level,
        reason=reason,
    )
    return result
