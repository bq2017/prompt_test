# -*- coding: utf-8 -*-
"""化学难度 V7（独立主定档 + 后置 Evidence-15）保守后处理。

设计目标
--------
1. 原始 difficulty_level 是主语义判断；Evidence-15 只作审计。
2. 不允许单个 feature、feature 数量或典型画像直接改档。
3. 每题最多执行一条规则，且最多相邻移动一档。
4. 自动规则必须同时得到：
   - 教师五维主判断；
   - rated_target / reasoning 中的具体任务结构；
   - 至少一个相互印证的审计字段；
   三类信息支持。
5. 默认 conservative_v1 只处理“主档位与模型自己给出的结构明显冲突”的窄场景。
6. 新 Prompt 上线初期应同时回放 prompt_only 与 conservative_v1；
   未经教师集和双跑稳定性验证，不应继续增加宽泛规则。

集成方式
--------
在现有批量脚本中：

    from chemistry_postprocess_v7_decoupled import postprocess_chemistry_difficulty
    final_result = postprocess_chemistry_difficulty(model_result, safe_question_data)

调用前应单独深拷贝并保存 model_result，作为 difficulty_rating_raw。
本模块不会读取输入数据中的 difficulty、label、teacher_label 等来源标签。
"""

from __future__ import annotations

import copy
import os
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

LEVELS: Tuple[str, ...] = ("送分题", "基础题", "中等题", "拔高题", "压轴题")
LEVEL_INDEX = {name: index for index, name in enumerate(LEVELS, 1)}

POSTPROCESS_PROFILE = (
    os.getenv("CHEMISTRY_V7_POSTPROCESS_PROFILE", "conservative_v1").strip().lower()
    or "conservative_v1"
)
VALID_PROFILES = {"prompt_only", "audit_only", "conservative_v1", "experimental_v1"}
if POSTPROCESS_PROFILE not in VALID_PROFILES:
    raise ValueError(
        f"未知 CHEMISTRY_V7_POSTPROCESS_PROFILE={POSTPROCESS_PROFILE!r}；"
        f"可选：{', '.join(sorted(VALID_PROFILES))}"
    )

PRIMARY_VALUES: Dict[str, Tuple[str, ...]] = {
    "dependency_structure": (
        "直接或透明对应",
        "单规则应用",
        "完整常规链",
        "存在决定性路径转换",
        "多高阶任务耦合",
    ),
    "decisive_transformation": (
        "无",
        "仅常规转换",
        "一个决定性卡点",
        "多个相互影响的决定性转换",
    ),
    "model_requirement": (
        "无模型",
        "单规则模型",
        "完整常规模型",
        "迁移高阶模型",
        "多模型耦合网络",
    ),
    "evidence_constraints": (
        "无实质证据约束",
        "单一直接条件",
        "联合证据或关联约束",
        "竞争解释或路径筛选",
        "多层嵌套",
    ),
    "coupling_structure": (
        "单一任务或独立多问",
        "规则切换但无纵向依赖",
        "共享题目特有模型",
        "存在结果复用",
        "多高阶任务深度耦合",
    ),
}

FEATURE_VALUES: Dict[str, Tuple[str, ...]] = {
    "entry_operation": (
        "直接检索",
        "一次透明映射",
        "自主选择一条规则",
        "形成中间结论后应用",
        "多节点连续决策",
    ),
    "task_rule_breadth": (
        "单一规则或同类检索束",
        "多个独立回答规则",
        "共享模型但无结果依赖",
        "存在结果或任务链依赖",
    ),
    "visual_information_role": (
        "无实质视觉信息",
        "仅呈现或重复文字",
        "直接识图作答",
        "提供局部关系",
        "决定模型、阶段或任务链",
    ),
    "reasoning_depth": ("0层", "1层", "2-3层", "4-5层", "6层及以上"),
    "reasoning_direction": ("直接识记", "正向推导", "逆向推导", "分类讨论或综合推导"),
    "knowledge_relation": (
        "单一知识点",
        "同模块简单关联",
        "同模块深度关联",
        "跨模块融合",
        "多模块深度融合",
    ),
    "representation_conversion": (
        "无",
        "一次表征转换",
        "两类表征连续转换",
        "宏观-微观-符号-定量多重转换",
    ),
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
    "experiment_requirement": (
        "无",
        "基础操作或读数",
        "控制变量、现象解释或数据归纳",
        "方案设计、评价或补充实验",
        "多阶段探究与定量误差",
    ),
    "graph_table_requirement": (
        "无",
        "直接读数",
        "多组比较归纳",
        "拐点、平台或分段反推",
        "多图表耦合建模",
    ),
    "calculation_model": (
        "无",
        "口算或直接比例",
        "单一方程式或关系式",
        "单一守恒或多反应计算",
        "多重守恒、差量、联立或分类",
    ),
    "unfamiliar_information_transfer": (
        "课内直接原型",
        "给定新信息直接应用",
        "迁移后建立关系",
        "完全陌生模型现场建立",
    ),
    "subquestion_dependency": (
        "无多问",
        "多问相互独立",
        "多问共享模型但无答案依赖",
        "多问存在结果或任务链依赖",
    ),
}

FEATURE_DEFAULTS = {field: values[0] for field, values in FEATURE_VALUES.items()}

# 只用于兼容旧 V6/V5.2 的少量同义值。别名只能规范格式，不能把自由文本“猜成”高阶值。
ALIASES: Dict[str, Dict[str, str]] = {
    "entry_operation": {
        "多步连续决策": "多节点连续决策",
    },
    "task_rule_breadth": {
        "多问存在前后依赖": "存在结果或任务链依赖",
        "多问共享模型但无答案依赖": "共享模型但无结果依赖",
    },
    "reasoning_depth": {
        "2—3层": "2-3层",
        "4—5层": "4-5层",
        "6层以上": "6层及以上",
    },
    "representation_conversion": {
        "两类表征往返": "两类表征连续转换",
    },
    "reaction_relation": {
        "单一反应": "单一直接反应",
        "先后或竞争反应": "先后、竞争或过量不足",
    },
    "constraint_complexity": {
        "多个关联约束": "多个相互关联约束",
    },
    "evidence_relation": {
        "无证据链": "无证据任务",
        "单一现象对应": "单一证据直接对应",
        "多条清晰证据链": "多条清晰证据联合",
        "干扰排除": "需要排除竞争解释",
        "证据冲突与筛选": "证据冲突、筛选或多层排除",
    },
    "experiment_requirement": {
        "控制变量或现象解释": "控制变量、现象解释或数据归纳",
        "方案设计或评价": "方案设计、评价或补充实验",
    },
    "graph_table_requirement": {
        "拐点或分段反推": "拐点、平台或分段反推",
    },
    "calculation_model": {
        "多重守恒差量联立或分类": "多重守恒、差量、联立或分类",
    },
    "subquestion_dependency": {
        "多问但相互独立": "多问相互独立",
        "多问且存在前后依赖": "多问存在结果或任务链依赖",
    },
}

REASONING_FIELDS = ("core_basis", "hard_point", "why_not_lower", "why_not_higher")


def _clean(value: Any) -> str:
    return str(value or "").strip().replace("\n", " ")


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", _clean(value)).replace("—", "-")


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def extract_raw_difficulty_level(result: Any) -> Optional[str]:
    if not isinstance(result, dict):
        return None
    top = result.get("difficulty_level")
    if top in LEVEL_INDEX:
        return str(top)
    reasoning = result.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("difficulty_level") in LEVEL_INDEX:
        return str(reasoning["difficulty_level"])
    return None


def _normalize_enum(
    field: str,
    value: Any,
    allowed: Sequence[str],
    aliases: Optional[Mapping[str, str]],
    flags: List[str],
    *,
    default: Optional[str] = None,
) -> Tuple[Optional[str], bool]:
    raw = _clean(value)
    if raw in allowed:
        return raw, True
    compact = _compact(raw)
    for candidate in allowed:
        if _compact(candidate) == compact:
            if raw != candidate:
                flags.append(f"{field}仅做格式规范化: {raw!r} -> {candidate!r}")
            return candidate, True
    if aliases:
        for alias, target in aliases.items():
            if _compact(alias) == compact and target in allowed:
                flags.append(f"{field}使用兼容别名: {raw!r} -> {target!r}")
                return target, True
    flags.append(f"{field}缺失或非法: {raw!r}")
    return default, False


def normalize_reasoning(result: Dict[str, Any]) -> None:
    source = _as_dict(result.get("reasoning"))
    result["reasoning"] = {field: _clean(source.get(field)) for field in REASONING_FIELDS}


def normalize_primary_dimensions(result: Dict[str, Any], flags: List[str]) -> Set[str]:
    source = _as_dict(result.get("primary_dimensions"))
    normalized: Dict[str, Any] = {}
    valid_fields: Set[str] = set()
    for field, allowed in PRIMARY_VALUES.items():
        item = _as_dict(source.get(field))
        level, valid = _normalize_enum(
            f"primary_dimensions.{field}.level",
            item.get("level"),
            allowed,
            None,
            flags,
            default=None,
        )
        normalized[field] = {
            "level": level,
            "basis": _clean(item.get("basis")),
        }
        if valid and normalized[field]["basis"]:
            valid_fields.add(field)
        elif valid:
            flags.append(f"primary_dimensions.{field}.basis为空，不能支持自动改档")
    result["primary_dimensions"] = normalized
    return valid_fields


def normalize_features(result: Dict[str, Any], flags: List[str]) -> Set[str]:
    source = _as_dict(result.get("features"))
    normalized: Dict[str, str] = {}
    valid_fields: Set[str] = set()
    for field, allowed in FEATURE_VALUES.items():
        value, valid = _normalize_enum(
            f"features.{field}",
            source.get(field),
            allowed,
            ALIASES.get(field),
            flags,
            default=FEATURE_DEFAULTS[field],
        )
        assert value is not None
        normalized[field] = value
        if valid:
            valid_fields.add(field)
    result["features"] = normalized
    return valid_fields


def normalize_rated_target(result: Dict[str, Any], flags: List[str]) -> None:
    source = _as_dict(result.get("rated_target"))
    relation, relation_valid = _normalize_enum(
        "rated_target.question_relation",
        source.get("question_relation"),
        FEATURE_VALUES["subquestion_dependency"],
        ALIASES.get("subquestion_dependency"),
        flags,
        default="无多问",
    )
    key_chain: List[str] = []
    for item in _as_list(source.get("key_chain"))[:8]:
        text = _clean(item)
        if text:
            key_chain.append(text)
    if not key_chain:
        flags.append("rated_target.key_chain为空，不能支持自动升档")
    result["rated_target"] = {
        "scope": _clean(source.get("scope")) or "整题",
        "task": _clean(source.get("task")),
        "question_relation": relation,
        "key_chain": key_chain,
        "decisive_link": _clean(source.get("decisive_link")) or "无",
    }
    if not relation_valid:
        flags.append("rated_target.question_relation回退为无多问")


def prepare_result(rating_result: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(rating_result, dict) or not rating_result:
        return {}
    result = copy.deepcopy(rating_result)
    raw_level = extract_raw_difficulty_level(result)

    input_assessment = _as_dict(result.get("input_assessment"))
    can_rate = input_assessment.get("can_rate") is not False
    result["input_assessment"] = {
        "can_rate": can_rate,
        "stem_readability": _clean(input_assessment.get("stem_readability")) or "完整",
        "solution_readability": _clean(input_assessment.get("solution_readability")) or "未提供但题面可独立定档",
        "missing_information": [
            _clean(item) for item in _as_list(input_assessment.get("missing_information")) if _clean(item)
        ],
        "conflict_description": _clean(input_assessment.get("conflict_description")) or "无",
    }

    result["postprocess_profile"] = POSTPROCESS_PROFILE
    result["postprocess_actions"] = []
    result["postprocess_candidate_flags"] = []
    result["schema_audit_flags"] = []

    normalize_reasoning(result)
    normalize_rated_target(result, result["schema_audit_flags"])

    if not can_rate:
        result["difficulty_level_raw"] = raw_level
        result["difficulty_level"] = None
        result["primary_dimensions"] = None
        result["features"] = None
        return result

    if raw_level not in LEVEL_INDEX:
        raise ValueError("模型未返回合法 difficulty_level")

    result["difficulty_level_raw"] = raw_level
    result["difficulty_level"] = raw_level
    valid_primary = normalize_primary_dimensions(result, result["schema_audit_flags"])
    valid_features = normalize_features(result, result["schema_audit_flags"])
    result["_valid_primary_fields"] = sorted(valid_primary)
    result["_valid_feature_fields"] = sorted(valid_features)

    _append_schema_consistency_audits(result)
    return result


def _primary(result: Mapping[str, Any], field: str) -> Optional[str]:
    item = _as_dict(_as_dict(result.get("primary_dimensions")).get(field))
    return item.get("level") if item.get("level") in PRIMARY_VALUES[field] else None


def _feature(result: Mapping[str, Any], field: str) -> Optional[str]:
    value = _as_dict(result.get("features")).get(field)
    return value if value in FEATURE_VALUES[field] else None


def _field_valid(result: Mapping[str, Any], field: str, *, primary: bool = False) -> bool:
    key = "_valid_primary_fields" if primary else "_valid_feature_fields"
    return field in set(result.get(key, []) or [])


def _rank(value: Optional[str], allowed: Sequence[str]) -> int:
    try:
        return list(allowed).index(value) if value is not None else -1
    except ValueError:
        return -1


def _at_least(result: Mapping[str, Any], field: str, minimum: str, *, primary: bool = False) -> bool:
    allowed = PRIMARY_VALUES[field] if primary else FEATURE_VALUES[field]
    value = _primary(result, field) if primary else _feature(result, field)
    return _rank(value, allowed) >= _rank(minimum, allowed)


def _decisive_link_is_concrete(result: Mapping[str, Any]) -> bool:
    link = _clean(_as_dict(result.get("rated_target")).get("decisive_link"))
    if not link or link in {"无", "没有", "无明显卡点", "不适用"}:
        return False
    generic = {"有关联", "综合性强", "前后联系", "存在依赖", "有影响"}
    if link in generic:
        return False
    return "→" in link or "->" in link or "决定" in link or "改变" in link or "影响" in link


def _key_chain(result: Mapping[str, Any]) -> List[str]:
    return [
        _clean(item)
        for item in _as_list(_as_dict(result.get("rated_target")).get("key_chain"))
        if _clean(item)
    ]


def _all_valid(result: Mapping[str, Any], primary_fields: Iterable[str], feature_fields: Iterable[str]) -> bool:
    return all(_field_valid(result, field, primary=True) for field in primary_fields) and all(
        _field_valid(result, field) for field in feature_fields
    )


def _append_schema_consistency_audits(result: Dict[str, Any]) -> None:
    flags: List[str] = result["schema_audit_flags"]
    level = result.get("difficulty_level_raw")
    dep = _primary(result, "dependency_structure")
    transform = _primary(result, "decisive_transformation")
    model = _primary(result, "model_requirement")
    coupling = _primary(result, "coupling_structure")

    if level == "送分题" and dep in {"完整常规链", "存在决定性路径转换", "多高阶任务耦合"}:
        flags.append("原始送分题与主维度中的完整链/路径转换明显冲突")
    if level == "压轴题" and coupling in {"单一任务或独立多问", "规则切换但无纵向依赖"}:
        flags.append("原始压轴题但主维度未识别高阶耦合")
    if transform in {"一个决定性卡点", "多个相互影响的决定性转换"} and not _decisive_link_is_concrete(result):
        flags.append("主维度声明决定性转换，但decisive_link缺少具体A→B关系")
    if model == "多模型耦合网络" and coupling != "多高阶任务深度耦合":
        flags.append("多模型耦合网络与整题耦合维度不一致")

    relation = _as_dict(result.get("rated_target")).get("question_relation")
    sub_feature = _feature(result, "subquestion_dependency")
    if relation and sub_feature and relation != sub_feature:
        flags.append(
            f"rated_target.question_relation={relation} 与 features.subquestion_dependency={sub_feature} 不一致"
        )


def _high_domain_supports(result: Mapping[str, Any]) -> List[str]:
    supports: List[str] = []
    if _feature(result, "reaction_relation") in {
        "多反应连续转化",
        "先后、竞争或过量不足",
        "需要分情况判断的反应模型",
    }:
        supports.append("高阶反应结构")
    if _feature(result, "evidence_relation") in {
        "需要排除竞争解释",
        "证据冲突、筛选或多层排除",
    }:
        supports.append("竞争解释或证据筛选")
    if _feature(result, "experiment_requirement") in {
        "方案设计、评价或补充实验",
        "多阶段探究与定量误差",
    }:
        supports.append("高阶实验任务")
    if _feature(result, "graph_table_requirement") in {
        "拐点、平台或分段反推",
        "多图表耦合建模",
    }:
        supports.append("图表反推或耦合")
    if _feature(result, "calculation_model") in {
        "单一守恒或多反应计算",
        "多重守恒、差量、联立或分类",
    }:
        supports.append("高阶定量模型")
    if _feature(result, "constraint_complexity") in {
        "多个相互关联约束",
        "多层嵌套约束",
    }:
        supports.append("关联或嵌套约束")
    if _feature(result, "unfamiliar_information_transfer") in {
        "迁移后建立关系",
        "完全陌生模型现场建立",
    }:
        supports.append("迁移或现场建模")
    return supports


def _complete_medium_domain_supports(result: Mapping[str, Any]) -> List[str]:
    supports: List[str] = []
    if _feature(result, "reaction_relation") in {
        "单一直接反应",
        "2-3个并列或简单连续反应",
        "多反应连续转化",
    }:
        supports.append("反应模型")
    if _feature(result, "evidence_relation") == "多条清晰证据联合":
        supports.append("清晰联合证据")
    if _feature(result, "experiment_requirement") == "控制变量、现象解释或数据归纳":
        supports.append("常规实验闭环")
    if _feature(result, "graph_table_requirement") == "多组比较归纳":
        supports.append("图表比较归纳")
    if _feature(result, "calculation_model") in {
        "单一方程式或关系式",
        "单一守恒或多反应计算",
    }:
        supports.append("完整常规定量模型")
    if _feature(result, "task_rule_breadth") in {
        "共享模型但无结果依赖",
        "存在结果或任务链依赖",
    }:
        supports.append("共享模型或结果依赖")
    return supports


def _is_joint_low_structure(result: Mapping[str, Any]) -> Tuple[bool, List[str]]:
    primary_fields = (
        "dependency_structure",
        "decisive_transformation",
        "model_requirement",
        "evidence_constraints",
        "coupling_structure",
    )
    feature_fields = (
        "entry_operation",
        "task_rule_breadth",
        "reasoning_depth",
        "reaction_relation",
        "experiment_requirement",
        "graph_table_requirement",
        "calculation_model",
        "constraint_complexity",
    )
    if not _all_valid(result, primary_fields, feature_fields):
        return False, []
    low_primary = (
        _primary(result, "dependency_structure") in {"直接或透明对应", "单规则应用"}
        and _primary(result, "decisive_transformation") in {"无", "仅常规转换"}
        and _primary(result, "model_requirement") in {"无模型", "单规则模型"}
        and _primary(result, "evidence_constraints") in {"无实质证据约束", "单一直接条件"}
        and _primary(result, "coupling_structure") in {
            "单一任务或独立多问",
            "规则切换但无纵向依赖",
        }
    )
    low_features = (
        _feature(result, "entry_operation") in {"直接检索", "一次透明映射", "自主选择一条规则"}
        and _feature(result, "reasoning_depth") in {"0层", "1层"}
        and _feature(result, "reaction_relation") in {"无反应关系", "单一直接反应"}
        and _feature(result, "experiment_requirement") in {"无", "基础操作或读数"}
        and _feature(result, "graph_table_requirement") in {"无", "直接读数"}
        and _feature(result, "calculation_model") in {"无", "口算或直接比例"}
        and _feature(result, "constraint_complexity") in {"无约束", "单一约束"}
        and not _decisive_link_is_concrete(result)
    )
    return low_primary and low_features, [
        "主维度仅为直接/单规则结构",
        "无决定性路径转换",
        "审计字段未识别完整模型或高阶任务",
    ]


def rule_easy_to_basic(result: Mapping[str, Any]) -> Tuple[bool, List[str]]:
    if result.get("difficulty_level_raw") != "送分题":
        return False, []
    required_primary = ("dependency_structure", "model_requirement")
    required_features = ("entry_operation", "reasoning_depth")
    if not _all_valid(result, required_primary, required_features):
        return False, []
    application = (
        _primary(result, "dependency_structure") != "直接或透明对应"
        and _primary(result, "model_requirement") != "无模型"
        and _feature(result, "entry_operation") not in {"直接检索", "一次透明映射"}
        and _feature(result, "reasoning_depth") != "0层"
    )
    return application, [
        "教师主维度已识别显性规则应用或更高结构",
        "后置审计也不是直接检索/0层",
        "原始送分档与模型自身结构判断明显冲突",
    ]


def rule_basic_to_easy(result: Mapping[str, Any]) -> Tuple[bool, List[str]]:
    if result.get("difficulty_level_raw") != "基础题":
        return False, []
    low, evidence = _is_joint_low_structure(result)
    strict_easy = (
        low
        and _primary(result, "dependency_structure") == "直接或透明对应"
        and _primary(result, "model_requirement") == "无模型"
        and _primary(result, "coupling_structure") == "单一任务或独立多问"
        and _feature(result, "entry_operation") in {"直接检索", "一次透明映射"}
        and _feature(result, "reasoning_depth") == "0层"
        and _feature(result, "task_rule_breadth") == "单一规则或同类检索束"
    )
    return strict_easy, evidence + ["同一检索规则且没有自主规则选择"]


def rule_basic_to_medium(result: Mapping[str, Any]) -> Tuple[bool, List[str]]:
    if result.get("difficulty_level_raw") != "基础题":
        return False, []
    required_primary = ("dependency_structure", "model_requirement")
    if not _all_valid(result, required_primary, ()):
        return False, []
    complete_primary = (
        _primary(result, "dependency_structure") in {
            "完整常规链",
            "存在决定性路径转换",
            "多高阶任务耦合",
        }
        and _primary(result, "model_requirement") in {
            "完整常规模型",
            "迁移高阶模型",
            "多模型耦合网络",
        }
    )
    supports = _complete_medium_domain_supports(result)
    chain_support = len(_key_chain(result)) >= 2
    ok = complete_primary and bool(supports) and chain_support
    return ok, ["主维度已识别完整常规链与完整模型", *supports[:3], "key_chain记录连续必要任务"]


def rule_medium_to_basic(result: Mapping[str, Any]) -> Tuple[bool, List[str]]:
    if result.get("difficulty_level_raw") != "中等题":
        return False, []
    low, evidence = _is_joint_low_structure(result)
    return low, evidence


def rule_medium_to_hard(result: Mapping[str, Any]) -> Tuple[bool, List[str]]:
    if result.get("difficulty_level_raw") != "中等题":
        return False, []
    required_primary = (
        "dependency_structure",
        "decisive_transformation",
        "model_requirement",
    )
    if not _all_valid(result, required_primary, ()):
        return False, []
    decisive_primary = (
        _primary(result, "dependency_structure") in {
            "存在决定性路径转换",
            "多高阶任务耦合",
        }
        and _primary(result, "decisive_transformation") in {
            "一个决定性卡点",
            "多个相互影响的决定性转换",
        }
        and _primary(result, "model_requirement") in {
            "迁移高阶模型",
            "多模型耦合网络",
        }
    )
    supports = _high_domain_supports(result)
    ok = decisive_primary and _decisive_link_is_concrete(result) and bool(supports)
    return ok, [
        "主维度识别决定性路径转换",
        "decisive_link明确写出前一判断如何改变后一任务",
        *supports[:4],
    ]


def rule_hard_to_final(result: Mapping[str, Any]) -> Tuple[bool, List[str]]:
    if result.get("difficulty_level_raw") != "拔高题":
        return False, []
    required_primary = (
        "dependency_structure",
        "decisive_transformation",
        "model_requirement",
        "coupling_structure",
    )
    if not _all_valid(result, required_primary, ()):
        return False, []
    deep_primary = (
        _primary(result, "dependency_structure") == "多高阶任务耦合"
        and _primary(result, "decisive_transformation") == "多个相互影响的决定性转换"
        and _primary(result, "model_requirement") == "多模型耦合网络"
        and _primary(result, "coupling_structure") == "多高阶任务深度耦合"
    )
    supports = _high_domain_supports(result)
    # 至少两个不同任务域支持，但不是简单“高阶feature计数”：
    # 四个主维度必须先完整命中，审计字段只做相互印证。
    ok = deep_primary and _decisive_link_is_concrete(result) and len(set(supports)) >= 2
    return ok, [
        "四个主维度共同指向多高阶任务深度耦合",
        "存在具体的跨任务决定性关系",
        *supports[:5],
    ]


def rule_hard_to_medium_experimental(result: Mapping[str, Any]) -> Tuple[bool, List[str]]:
    if result.get("difficulty_level_raw") != "拔高题":
        return False, []
    low, evidence = _is_joint_low_structure(result)
    conventional = (
        _primary(result, "dependency_structure") == "完整常规链"
        and _primary(result, "decisive_transformation") in {"无", "仅常规转换"}
        and _primary(result, "model_requirement") == "完整常规模型"
        and not _decisive_link_is_concrete(result)
        and not _high_domain_supports(result)
    )
    return low or conventional, evidence + ["未找到改变后续路径的具体卡点"]


def rule_final_to_hard_experimental(result: Mapping[str, Any]) -> Tuple[bool, List[str]]:
    if result.get("difficulty_level_raw") != "压轴题":
        return False, []
    # 特殊教师锚点可能不满足一般耦合画像，因此默认 profile 不执行此规则。
    no_deep_coupling = (
        _primary(result, "dependency_structure") != "多高阶任务耦合"
        and _primary(result, "decisive_transformation") != "多个相互影响的决定性转换"
        and _primary(result, "model_requirement") != "多模型耦合网络"
        and _primary(result, "coupling_structure") != "多高阶任务深度耦合"
        and len(_high_domain_supports(result)) < 2
    )
    return no_deep_coupling, ["一般压轴耦合画像缺失；注意需人工排除特殊教师真同构锚点"]


def _set_level(result: Dict[str, Any], target: str, rule: str, evidence: Sequence[str]) -> None:
    raw = result.get("difficulty_level_raw")
    if raw not in LEVEL_INDEX or target not in LEVEL_INDEX:
        raise ValueError("非法后处理档位")
    if abs(LEVEL_INDEX[target] - LEVEL_INDEX[raw]) != 1:
        raise ValueError("后处理只能相邻移动一档")
    if result.get("postprocess_actions"):
        raise ValueError("每题最多执行一条后处理规则")
    result["difficulty_level"] = target
    result["postprocess_actions"].append(
        {
            "rule": rule,
            "from": raw,
            "to": target,
            "evidence": [str(item) for item in evidence if str(item).strip()][:8],
        }
    )

    reasoning = _as_dict(result.get("reasoning"))
    prefix = f"后处理相邻校准：{rule}；依据：{'；'.join(evidence[:5])}。"
    reasoning["core_basis"] = prefix + _clean(reasoning.get("core_basis"))
    lower, higher = adjacent_levels(target)
    reasoning["why_not_lower"] = (
        "已是最低档。" if lower is None else f"联合结构证据超过{lower}边界，详见postprocess_actions。"
    )
    reasoning["why_not_higher"] = (
        "已是最高档。" if higher is None else f"当前校准只修复与{target}相邻的明确偏差，不据审计字段继续升到{higher}。"
    )
    result["reasoning"] = reasoning


def adjacent_levels(level: str) -> Tuple[Optional[str], Optional[str]]:
    index = LEVEL_INDEX[level] - 1
    lower = LEVELS[index - 1] if index > 0 else None
    higher = LEVELS[index + 1] if index + 1 < len(LEVELS) else None
    return lower, higher


def _collect_candidate_flags(result: Dict[str, Any]) -> None:
    candidates = [
        ("easy_to_basic_clear_application", rule_easy_to_basic),
        ("basic_to_easy_joint_direct", rule_basic_to_easy),
        ("basic_to_medium_complete_model", rule_basic_to_medium),
        ("medium_to_basic_joint_low_structure", rule_medium_to_basic),
        ("medium_to_hard_decisive_path", rule_medium_to_hard),
        ("hard_to_final_deep_coupling", rule_hard_to_final),
        ("hard_to_medium_missing_decisive_experimental", rule_hard_to_medium_experimental),
        ("final_to_hard_missing_coupling_experimental", rule_final_to_hard_experimental),
    ]
    for name, fn in candidates:
        ok, evidence = fn(result)
        if ok:
            result["postprocess_candidate_flags"].append(
                {"rule": name, "evidence": evidence[:8]}
            )


def postprocess_conservative_v1(result: Dict[str, Any]) -> Dict[str, Any]:
    """默认窄规则。按 raw_level 分支，命中后立即停止。"""
    raw = result.get("difficulty_level_raw")

    if raw == "送分题":
        ok, evidence = rule_easy_to_basic(result)
        if ok:
            _set_level(result, "基础题", "easy_to_basic_clear_application", evidence)
            return result

    elif raw == "基础题":
        # 同一原始档只能命中一个方向；先保护完整常规模型的严重低估，
        # 再处理全维度一致的直接题高估。
        ok, evidence = rule_basic_to_medium(result)
        if ok:
            _set_level(result, "中等题", "basic_to_medium_complete_model", evidence)
            return result
        ok, evidence = rule_basic_to_easy(result)
        if ok:
            _set_level(result, "送分题", "basic_to_easy_joint_direct", evidence)
            return result

    elif raw == "中等题":
        ok, evidence = rule_medium_to_hard(result)
        if ok:
            _set_level(result, "拔高题", "medium_to_hard_decisive_path", evidence)
            return result
        ok, evidence = rule_medium_to_basic(result)
        if ok:
            _set_level(result, "基础题", "medium_to_basic_joint_low_structure", evidence)
            return result

    elif raw == "拔高题":
        ok, evidence = rule_hard_to_final(result)
        if ok:
            _set_level(result, "压轴题", "hard_to_final_deep_coupling", evidence)
            return result

    # conservative_v1 不自动把拔高降中等，也不把压轴降拔高；
    # 两条规则需要先排除教师特殊锚点并完成教师集回放。
    return result


def postprocess_experimental_v1(result: Dict[str, Any]) -> Dict[str, Any]:
    result = postprocess_conservative_v1(result)
    if result.get("postprocess_actions"):
        return result
    raw = result.get("difficulty_level_raw")
    if raw == "拔高题":
        ok, evidence = rule_hard_to_medium_experimental(result)
        if ok:
            _set_level(result, "中等题", "hard_to_medium_missing_decisive_experimental", evidence)
    elif raw == "压轴题":
        ok, evidence = rule_final_to_hard_experimental(result)
        if ok:
            _set_level(result, "拔高题", "final_to_hard_missing_coupling_experimental", evidence)
    return result


def _strip_internal_fields(result: Dict[str, Any]) -> None:
    result.pop("_valid_primary_fields", None)
    result.pop("_valid_feature_fields", None)


def validate_postprocess_result(result: Dict[str, Any]) -> Dict[str, Any]:
    if not result:
        return result
    if result.get("input_assessment", {}).get("can_rate") is False:
        if result.get("difficulty_level") is not None:
            raise ValueError("can_rate=false时difficulty_level必须为null")
        return result

    raw = result.get("difficulty_level_raw")
    final = result.get("difficulty_level")
    if raw not in LEVEL_INDEX or final not in LEVEL_INDEX:
        raise ValueError("后处理缺少合法raw/final difficulty_level")
    actions = result.get("postprocess_actions", [])
    if len(actions) > 1:
        raise ValueError("后处理每题最多执行一条规则")
    if abs(LEVEL_INDEX[raw] - LEVEL_INDEX[final]) > 1:
        raise ValueError("后处理最多相邻调整一档")
    if actions:
        action = actions[0]
        if action.get("from") != raw or action.get("to") != final:
            raise ValueError("postprocess_actions与raw/final档位不一致")
    if not isinstance(result.get("features"), dict) or set(result["features"]) != set(FEATURE_VALUES):
        raise ValueError("features必须恰好包含15个字段")
    return result


def postprocess_chemistry_difficulty(
    rating_result: Dict[str, Any],
    data: Optional[Dict[str, Any]] = None,
    *,
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    """统一入口。data保留接口兼容，目前不使用题型关键词做自动改档。"""
    _ = data or {}
    selected = (profile or POSTPROCESS_PROFILE).strip().lower()
    if selected not in VALID_PROFILES:
        raise ValueError(f"未知后处理profile: {selected}")

    result = prepare_result(rating_result, _)
    if not result:
        return result
    result["postprocess_profile"] = selected

    if result.get("input_assessment", {}).get("can_rate") is False:
        _strip_internal_fields(result)
        return validate_postprocess_result(result)

    _collect_candidate_flags(result)

    if selected in {"prompt_only", "audit_only"}:
        pass
    elif selected == "conservative_v1":
        result = postprocess_conservative_v1(result)
    elif selected == "experimental_v1":
        result = postprocess_experimental_v1(result)

    _strip_internal_fields(result)
    return validate_postprocess_result(result)


# -------------------------- 最小自检 --------------------------

def _base_result(level: str) -> Dict[str, Any]:
    return {
        "input_assessment": {
            "can_rate": True,
            "stem_readability": "完整",
            "solution_readability": "完整",
            "missing_information": [],
            "conflict_description": "无",
        },
        "rated_target": {
            "scope": "整题",
            "task": "测试任务",
            "question_relation": "无多问",
            "key_chain": ["测试任务"],
            "decisive_link": "无",
        },
        "primary_dimensions": {
            "dependency_structure": {"level": "直接或透明对应", "basis": "测试"},
            "decisive_transformation": {"level": "无", "basis": "测试"},
            "model_requirement": {"level": "无模型", "basis": "测试"},
            "evidence_constraints": {"level": "无实质证据约束", "basis": "测试"},
            "coupling_structure": {"level": "单一任务或独立多问", "basis": "测试"},
        },
        "reasoning": {
            "core_basis": "测试",
            "hard_point": "无明显卡点",
            "why_not_lower": "测试",
            "why_not_higher": "测试",
        },
        "difficulty_level": level,
        "features": copy.deepcopy(FEATURE_DEFAULTS),
    }


def run_self_tests() -> None:
    # 基础 -> 送分：所有主维度和审计字段都为直接结构。
    result = postprocess_chemistry_difficulty(_base_result("基础题"), profile="conservative_v1")
    assert result["difficulty_level"] == "送分题"
    assert len(result["postprocess_actions"]) == 1

    # 中等 -> 拔高：必须有明确决定性路径和高阶任务域支持。
    hard = _base_result("中等题")
    hard["rated_target"]["key_chain"] = ["判断过量状态", "确定滤液组成", "选择计算模型"]
    hard["rated_target"]["decisive_link"] = "过量状态 → 改变滤液组成和后续计算模型"
    hard["primary_dimensions"]["dependency_structure"] = {"level": "存在决定性路径转换", "basis": "过量改变后续"}
    hard["primary_dimensions"]["decisive_transformation"] = {"level": "一个决定性卡点", "basis": "过量"}
    hard["primary_dimensions"]["model_requirement"] = {"level": "迁移高阶模型", "basis": "分阶段反应"}
    hard["features"]["reaction_relation"] = "先后、竞争或过量不足"
    hard["features"]["constraint_complexity"] = "多个相互关联约束"
    hard_result = postprocess_chemistry_difficulty(hard, profile="conservative_v1")
    assert hard_result["difficulty_level"] == "拔高题"

    # 单字段高值不能触发。
    noisy = _base_result("中等题")
    noisy["features"]["reasoning_depth"] = "6层及以上"
    noisy_result = postprocess_chemistry_difficulty(noisy, profile="conservative_v1")
    assert noisy_result["difficulty_level"] == "中等题"  # 单个高值既不能抬档，也不会被规则强行覆盖
    assert noisy_result["postprocess_actions"] == []

    # prompt_only永远保留原档。
    raw = postprocess_chemistry_difficulty(_base_result("基础题"), profile="prompt_only")
    assert raw["difficulty_level"] == "基础题"
    assert raw["postprocess_actions"] == []

    # 每题最多一条、最多相邻一档。
    for sample in (result, hard_result, noisy_result, raw):
        validate_postprocess_result(sample)


if __name__ == "__main__":
    run_self_tests()
    print("chemistry_postprocess_v7_decoupled self-tests passed")
