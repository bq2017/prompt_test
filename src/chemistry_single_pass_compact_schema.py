"""Strict schema and audit-only guards for chemistry single-pass compact A3."""

from __future__ import annotations

import copy
from typing import Any, Mapping


LEVELS = ("送分题", "基础题", "中等题", "拔高题", "压轴题")
LEVEL_RANK = {level: index for index, level in enumerate(LEVELS, 1)}
BOUNDARY_NEIGHBORS = {
    "送分题": (None, "基础题"),
    "基础题": ("送分题", "中等题"),
    "中等题": ("基础题", "拔高题"),
    "拔高题": ("中等题", "压轴题"),
    "压轴题": ("拔高题", None),
}

TOP_FIELDS = {
    "input_assessment", "rated_target", "task_structure", "rating_dimensions",
    "boundary_review", "decisive_reason", "special_anchor", "difficulty_level",
}
INPUT_FIELDS = {
    "can_rate", "stem_readability", "solution_readability",
    "missing_information", "conflict_description",
}
TARGET_FIELDS = {"scope", "task", "included_prerequisites"}
STRUCTURE_FIELDS = {
    "question_relation", "shared_model", "decisive_dependencies", "structure_summary",
}
SHARED_FIELDS = {"present", "description"}
DEPENDENCY_FIELDS = {"from", "to", "basis"}
DIMENSION_FIELDS = {
    "dependency_demand", "model_demand", "evidence_constraint_demand",
    "quantitative_demand", "transfer_demand",
}
BOUNDARY_FIELDS = {"why_not_lower", "why_not_higher"}
ANCHOR_FIELDS = {"type", "evidence"}

STEM_READABILITY = ("完整", "局部缺失但可定档", "关键缺失无法定档")
SOLUTION_READABILITY = (
    "完整", "局部缺失但不影响定档", "未提供但题面可独立定档",
    "关键缺失但题面仍可定档", "与题干存在冲突",
)
QUESTION_RELATIONS = (
    "无多问", "多问相互独立", "多问共享模型但无结果依赖",
    "多问存在结果或任务链依赖",
)
DIMENSION_ENUMS = {
    "dependency_demand": (
        "直接识别", "单一显性应用", "常规连续模型",
        "决定性高阶任务边", "多高阶任务耦合",
    ),
    "model_demand": (
        "无需模型", "单一显性规则", "完整常规模型",
        "含路径转换的高阶模型", "多模型耦合网络",
    ),
    "evidence_constraint_demand": (
        "无证据约束任务", "单一显性条件", "常规联合证据或约束",
        "竞争解释或关键约束改变路径", "多阶段证据约束闭环",
    ),
    "quantitative_demand": ("无定量", "直接代入", "常规单模型", "高阶单模型", "多模型耦合定量"),
    "transfer_demand": ("课内原型", "给定信息直接用", "迁移建模", "现场建模"),
}


class CompactSchemaError(ValueError):
    """The model output violates the A3 compact contract."""


class UnrateableInputError(CompactSchemaError):
    """The model correctly refuses to rate because key stem facts are missing."""


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CompactSchemaError(f"{name}必须是JSON对象")
    return dict(value)


def _exact(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        raise CompactSchemaError(f"{name}字段不匹配: missing={missing}, extra={extra}")


def _text(value: Any, name: str, maximum: int = 1400) -> str:
    text = str(value or "").strip()
    if not text:
        raise CompactSchemaError(f"{name}不能为空")
    if len(text) > maximum:
        raise CompactSchemaError(f"{name}过长: {len(text)} > {maximum}")
    return text


def _text_list(value: Any, name: str, maximum: int, item_limit: int = 500) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise CompactSchemaError(f"{name}必须是最多{maximum}项的数组")
    return [_text(item, f"{name}[{index}]", item_limit) for index, item in enumerate(value)]


def _validate_input(value: Any) -> dict[str, Any]:
    obj = _mapping(value, "input_assessment")
    _exact(obj, INPUT_FIELDS, "input_assessment")
    if not isinstance(obj["can_rate"], bool):
        raise CompactSchemaError("input_assessment.can_rate必须是布尔值")
    if obj["stem_readability"] not in STEM_READABILITY:
        raise CompactSchemaError("stem_readability枚举非法")
    if obj["solution_readability"] not in SOLUTION_READABILITY:
        raise CompactSchemaError("solution_readability枚举非法")
    return {
        "can_rate": obj["can_rate"],
        "stem_readability": obj["stem_readability"],
        "solution_readability": obj["solution_readability"],
        "missing_information": _text_list(obj["missing_information"], "missing_information", 10, 300),
        "conflict_description": _text(obj["conflict_description"], "conflict_description", 600),
    }


def _validate_target(value: Any) -> dict[str, Any]:
    obj = _mapping(value, "rated_target")
    _exact(obj, TARGET_FIELDS, "rated_target")
    return {
        "scope": _text(obj["scope"], "rated_target.scope", 300),
        "task": _text(obj["task"], "rated_target.task", 900),
        "included_prerequisites": _text_list(obj["included_prerequisites"], "included_prerequisites", 12, 500),
    }


def _validate_structure(value: Any) -> dict[str, Any]:
    obj = _mapping(value, "task_structure")
    _exact(obj, STRUCTURE_FIELDS, "task_structure")
    if obj["question_relation"] not in QUESTION_RELATIONS:
        raise CompactSchemaError("task_structure.question_relation枚举非法")
    shared = _mapping(obj["shared_model"], "shared_model")
    _exact(shared, SHARED_FIELDS, "shared_model")
    if not isinstance(shared["present"], bool):
        raise CompactSchemaError("shared_model.present必须是布尔值")
    description = _text(shared["description"], "shared_model.description", 900)
    if obj["question_relation"] == "多问共享模型但无结果依赖" and not shared["present"]:
        raise CompactSchemaError("多问共享模型关系要求shared_model.present=true")
    raw_dependencies = obj["decisive_dependencies"]
    if not isinstance(raw_dependencies, list) or len(raw_dependencies) > 2:
        raise CompactSchemaError("decisive_dependencies必须是最多2项的数组")
    dependencies: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_dependencies):
        dep = _mapping(raw, f"decisive_dependencies[{index}]")
        _exact(dep, DEPENDENCY_FIELDS, f"decisive_dependencies[{index}]")
        source = _text(dep["from"], f"decisive_dependencies[{index}].from", 500)
        target = _text(dep["to"], f"decisive_dependencies[{index}].to", 500)
        if source == target or (source, target) in seen:
            raise CompactSchemaError("decisive_dependencies存在自依赖或重复")
        seen.add((source, target))
        dependencies.append({
            "from": source,
            "to": target,
            "basis": _text(dep["basis"], f"decisive_dependencies[{index}].basis", 900),
        })
    return {
        "question_relation": obj["question_relation"],
        "shared_model": {"present": shared["present"], "description": description},
        "decisive_dependencies": dependencies,
        "structure_summary": _text(obj["structure_summary"], "structure_summary", 1400),
    }


def _validate_dimensions(value: Any) -> dict[str, str]:
    obj = _mapping(value, "rating_dimensions")
    _exact(obj, DIMENSION_FIELDS, "rating_dimensions")
    result: dict[str, str] = {}
    for field, allowed in DIMENSION_ENUMS.items():
        if obj[field] not in allowed:
            raise CompactSchemaError(f"rating_dimensions.{field}枚举非法")
        result[field] = obj[field]
    return result


def _audit_flags(
    level: str,
    structure: Mapping[str, Any],
    dimensions: Mapping[str, str],
    anchor_type: str,
) -> list[dict[str, Any]]:
    dependencies = structure["decisive_dependencies"]
    shared = structure["shared_model"]["present"]
    low_dimensions = (
        dimensions["dependency_demand"] in ("直接识别", "单一显性应用")
        and dimensions["model_demand"] in ("无需模型", "单一显性规则")
        and dimensions["evidence_constraint_demand"] in ("无证据约束任务", "单一显性条件")
        and dimensions["quantitative_demand"] in ("无定量", "直接代入")
        and dimensions["transfer_demand"] in ("课内原型", "给定信息直接用")
    )
    flags: list[dict[str, Any]] = []
    if LEVEL_RANK[level] >= 3 and not dependencies and not shared and low_dimensions:
        flags.append({
            "code": "low_structure_overrating_candidate",
            "reason": "中等及以上档位缺少决定性依赖和共享模型，且五个审计维度均为低结构。",
        })
    if level == "拔高题" and not dependencies:
        flags.append({
            "code": "hard_without_decisive_dependency_candidate",
            "reason": "拔高题未提供任何A判断改变B任务的决定性依赖。",
        })
    if level == "压轴题" and anchor_type != "固定基团教师锚点" and len(dependencies) < 2:
        flags.append({
            "code": "final_without_coupling_candidate",
            "reason": "非特殊锚点压轴题不足两条决定性依赖，需复核是否只是单一卡点。",
        })
    return flags


def validate_and_prepare_compact_result(value: Any) -> dict[str, Any]:
    obj = _mapping(value, "compact_rating")
    _exact(obj, TOP_FIELDS, "compact_rating")
    assessment = _validate_input(obj["input_assessment"])
    if not assessment["can_rate"]:
        if assessment["stem_readability"] != "关键缺失无法定档":
            raise CompactSchemaError("can_rate=false时stem_readability必须为关键缺失无法定档")
        for field in ("rated_target", "task_structure", "rating_dimensions", "boundary_review", "special_anchor", "difficulty_level"):
            if obj[field] is not None:
                raise CompactSchemaError(f"不可定档时{field}必须为null")
        _text(obj["decisive_reason"], "decisive_reason", 1000)
        raise UnrateableInputError("题干关键缺失，拒绝猜测档位")
    if assessment["stem_readability"] == "关键缺失无法定档":
        raise CompactSchemaError("can_rate=true与关键缺失无法定档矛盾")

    level = obj["difficulty_level"]
    if level not in LEVELS:
        raise CompactSchemaError("difficulty_level枚举非法")
    target = _validate_target(obj["rated_target"])
    structure = _validate_structure(obj["task_structure"])
    dimensions = _validate_dimensions(obj["rating_dimensions"])
    boundary = _mapping(obj["boundary_review"], "boundary_review")
    _exact(boundary, BOUNDARY_FIELDS, "boundary_review")
    anchor = _mapping(obj["special_anchor"], "special_anchor")
    _exact(anchor, ANCHOR_FIELDS, "special_anchor")
    if anchor["type"] not in ("无", "固定基团教师锚点"):
        raise CompactSchemaError("special_anchor.type枚举非法")
    anchor_evidence = _text(anchor["evidence"], "special_anchor.evidence", 1000)
    lower, upper = BOUNDARY_NEIGHBORS[level]
    flags = _audit_flags(level, structure, dimensions, anchor["type"])
    return {
        "input_assessment": assessment,
        "rated_target": target,
        "task_structure": structure,
        "rating_dimensions": dimensions,
        "boundary_review": {
            "lower_level": lower,
            "why_not_lower": _text(boundary["why_not_lower"], "why_not_lower", 1200),
            "upper_level": upper,
            "why_not_higher": _text(boundary["why_not_higher"], "why_not_higher", 1200),
        },
        "decisive_reason": _text(obj["decisive_reason"], "decisive_reason", 1400),
        "special_anchor": {"type": anchor["type"], "evidence": anchor_evidence},
        "difficulty_level": level,
        "postprocess_original_level": level,
        "postprocess_final_level": level,
        "postprocess_actions": [],
        "automatic_level_change_applied": False,
        "a3_audit_only": True,
        "a3_audit_flags": flags,
        "a3_audit_flag_count": len(flags),
    }


def apply_observe_only_audit(prepared: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy while explicitly preserving the original model level."""
    result = copy.deepcopy(dict(prepared))
    original = result["postprocess_original_level"]
    result["difficulty_level"] = original
    result["postprocess_final_level"] = original
    result["postprocess_actions"] = []
    result["automatic_level_change_applied"] = False
    return result
