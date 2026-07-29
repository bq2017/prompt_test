"""Strict schema and observe-only postprocessing for chemistry V8."""

from __future__ import annotations

import copy
from collections import Counter, deque
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
    "input_assessment", "rated_target", "primary_dimensions", "task_graph",
    "audit_attributes", "boundary_review", "decisive_reason", "difficulty_level",
}
INPUT_FIELDS = {
    "can_rate", "stem_readability", "solution_readability",
    "missing_information", "conflict_description",
}
TARGET_FIELDS = {"scope", "task", "included_prerequisites"}
DIMENSION_FIELDS = {
    "dependency_structure", "decisive_transformation", "model_requirement",
    "evidence_constraints", "coupling_structure",
}
DIMENSION_VALUE_FIELDS = {"level", "basis"}
GRAPH_FIELDS = {"nodes", "edges", "shared_models"}
NODE_FIELDS = {"id", "subquestion", "kind", "input", "action", "output", "source_evidence"}
EDGE_FIELDS = {"from", "to", "type", "transferred_object", "basis"}
SHARED_MODEL_FIELDS = {"id", "description", "node_ids", "basis"}
AUDIT_FIELDS = {
    "question_relation", "visual_role", "reaction_structure", "experiment_structure",
    "graph_table_structure", "calculation_structure", "information_transfer",
}
BOUNDARY_FIELDS = {"why_not_lower", "why_not_higher"}

STEM_READABILITY = ("完整", "局部缺失但可定档", "关键缺失无法定档")
SOLUTION_READABILITY = (
    "完整", "局部缺失但不影响定档", "未提供但题面可独立定档", "与题干存在冲突",
)
DIMENSION_ENUMS = {
    "dependency_structure": (
        "直接或透明对应", "单规则应用", "完整常规链", "存在决定性路径转换", "多高阶任务耦合",
    ),
    "decisive_transformation": (
        "无", "仅常规转换", "一个决定性卡点", "多个相互影响的决定性转换",
    ),
    "model_requirement": (
        "无模型", "单规则模型", "完整常规模型", "迁移高阶模型", "多模型耦合网络",
    ),
    "evidence_constraints": (
        "无实质证据约束", "单一直接条件", "联合证据或关联约束",
        "竞争解释或路径筛选", "多层嵌套",
    ),
    "coupling_structure": (
        "单一任务或独立多问", "共享题面但无模型依赖", "共享题目特有模型",
        "存在结果复用", "多高阶任务深度耦合",
    ),
}
NODE_KINDS = {
    "direct_retrieval", "rule_application", "reaction", "experiment", "evidence",
    "graph", "calculation", "constraint", "modeling",
}
EDGE_TYPES = {
    "result_reuse", "condition_dependency", "evidence_dependency", "model_selection_dependency",
}
AUDIT_ENUMS = {
    "question_relation": (
        "无多问", "多问相互独立", "多问共享模型但无答案依赖", "多问存在结果或任务链依赖",
    ),
    "visual_role": (
        "无实质视觉信息", "直接读取", "提供局部关系", "决定阶段、模型或任务链",
    ),
    "reaction_structure": (
        "无反应任务", "单一反应", "多个独立反应", "连续反应链", "先后、过量、竞争或分类",
    ),
    "experiment_structure": (
        "无实验任务", "基础操作或读数", "常规实验闭环", "方案评价或干扰排除", "多阶段探究与误差",
    ),
    "graph_table_structure": (
        "无图表任务", "直接读数", "比较归纳或显性阶段", "分段、拐点或平台反推", "多图表耦合",
    ),
    "calculation_structure": (
        "无计算", "直接比例", "常规单模型", "高阶守恒或多反应计算",
        "多重守恒、差量、联立、范围或分类",
    ),
    "information_transfer": (
        "课内直接原型", "给定新信息直接应用", "迁移后建立关系", "完全陌生模型现场建立",
    ),
}


class V8SchemaError(ValueError):
    """The model output violates the V8 full-task-graph contract."""


class UnrateableInputError(V8SchemaError):
    """The model correctly refuses to rate because stem facts are missing."""


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise V8SchemaError(f"{name}必须是JSON对象")
    return dict(value)


def _exact(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        raise V8SchemaError(f"{name}字段不匹配: missing={missing}, extra={extra}")


def _text(value: Any, name: str, maximum: int = 1600) -> str:
    text = str(value or "").strip()
    if not text:
        raise V8SchemaError(f"{name}不能为空")
    if len(text) > maximum:
        raise V8SchemaError(f"{name}过长: {len(text)} > {maximum}")
    return text


def _text_list(value: Any, name: str, maximum: int, item_limit: int = 600) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise V8SchemaError(f"{name}必须是最多{maximum}项的数组")
    return [_text(item, f"{name}[{index}]", item_limit) for index, item in enumerate(value)]


def _validate_assessment(value: Any) -> dict[str, Any]:
    obj = _mapping(value, "input_assessment")
    _exact(obj, INPUT_FIELDS, "input_assessment")
    if not isinstance(obj["can_rate"], bool):
        raise V8SchemaError("input_assessment.can_rate必须是布尔值")
    if obj["stem_readability"] not in STEM_READABILITY:
        raise V8SchemaError("stem_readability枚举非法")
    if obj["solution_readability"] not in SOLUTION_READABILITY:
        raise V8SchemaError("solution_readability枚举非法")
    return {
        "can_rate": obj["can_rate"],
        "stem_readability": obj["stem_readability"],
        "solution_readability": obj["solution_readability"],
        "missing_information": _text_list(obj["missing_information"], "missing_information", 12),
        "conflict_description": _text(obj["conflict_description"], "conflict_description", 800),
    }


def _validate_target(value: Any) -> dict[str, Any]:
    obj = _mapping(value, "rated_target")
    _exact(obj, TARGET_FIELDS, "rated_target")
    return {
        "scope": _text(obj["scope"], "rated_target.scope", 400),
        "task": _text(obj["task"], "rated_target.task", 1200),
        "included_prerequisites": _text_list(
            obj["included_prerequisites"], "rated_target.included_prerequisites", 20
        ),
    }


def _validate_dimensions(value: Any) -> dict[str, Any]:
    obj = _mapping(value, "primary_dimensions")
    _exact(obj, DIMENSION_FIELDS, "primary_dimensions")
    result: dict[str, Any] = {}
    for field, allowed in DIMENSION_ENUMS.items():
        item = _mapping(obj[field], f"primary_dimensions.{field}")
        _exact(item, DIMENSION_VALUE_FIELDS, f"primary_dimensions.{field}")
        if item["level"] not in allowed:
            raise V8SchemaError(f"primary_dimensions.{field}.level枚举非法")
        result[field] = {
            "level": item["level"],
            "basis": _text(item["basis"], f"primary_dimensions.{field}.basis", 1200),
        }
    return result


def _validate_graph(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    graph = _mapping(value, "task_graph")
    _exact(graph, GRAPH_FIELDS, "task_graph")
    raw_nodes = graph["nodes"]
    if not isinstance(raw_nodes, list) or len(raw_nodes) > 20:
        raise V8SchemaError("task_graph.nodes必须是最多20项的数组")
    nodes: list[dict[str, str]] = []
    by_id: dict[str, dict[str, str]] = {}
    for index, raw in enumerate(raw_nodes):
        node = _mapping(raw, f"nodes[{index}]")
        _exact(node, NODE_FIELDS, f"nodes[{index}]")
        node_id = _text(node["id"], f"nodes[{index}].id", 80)
        if node_id in by_id:
            raise V8SchemaError(f"节点ID重复: {node_id}")
        if node["kind"] not in NODE_KINDS:
            raise V8SchemaError(f"nodes[{index}].kind枚举非法")
        prepared = {
            "id": node_id,
            "subquestion": _text(node["subquestion"], f"nodes[{index}].subquestion", 300),
            "kind": node["kind"],
            "input": _text(node["input"], f"nodes[{index}].input", 1200),
            "action": _text(node["action"], f"nodes[{index}].action", 1200),
            "output": _text(node["output"], f"nodes[{index}].output", 1200),
            "source_evidence": _text(node["source_evidence"], f"nodes[{index}].source_evidence", 1400),
        }
        nodes.append(prepared)
        by_id[node_id] = prepared

    raw_edges = graph["edges"]
    if not isinstance(raw_edges, list) or len(raw_edges) > 60:
        raise V8SchemaError("task_graph.edges必须是最多60项的数组")
    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(raw_edges):
        edge = _mapping(raw, f"edges[{index}]")
        _exact(edge, EDGE_FIELDS, f"edges[{index}]")
        source = _text(edge["from"], f"edges[{index}].from", 80)
        target = _text(edge["to"], f"edges[{index}].to", 80)
        edge_type = edge["type"]
        if source not in by_id or target not in by_id:
            raise V8SchemaError(f"edges[{index}]引用不存在节点")
        if source == target:
            raise V8SchemaError(f"edges[{index}]不允许自依赖")
        if edge_type not in EDGE_TYPES:
            raise V8SchemaError(f"edges[{index}].type枚举非法")
        key = (source, target, edge_type)
        if key in seen_edges:
            raise V8SchemaError(f"edges[{index}]重复")
        seen_edges.add(key)
        edges.append({
            "from": source,
            "to": target,
            "type": edge_type,
            "transferred_object": _text(
                edge["transferred_object"], f"edges[{index}].transferred_object", 900
            ),
            "basis": _text(edge["basis"], f"edges[{index}].basis", 1200),
        })

    raw_shared = graph["shared_models"]
    if not isinstance(raw_shared, list) or len(raw_shared) > 20:
        raise V8SchemaError("task_graph.shared_models必须是最多20项的数组")
    shared_models: list[dict[str, Any]] = []
    shared_ids: set[str] = set()
    for index, raw in enumerate(raw_shared):
        shared = _mapping(raw, f"shared_models[{index}]")
        _exact(shared, SHARED_MODEL_FIELDS, f"shared_models[{index}]")
        shared_id = _text(shared["id"], f"shared_models[{index}].id", 80)
        if shared_id in shared_ids:
            raise V8SchemaError(f"共享模型ID重复: {shared_id}")
        shared_ids.add(shared_id)
        node_ids = _text_list(shared["node_ids"], f"shared_models[{index}].node_ids", 20, 80)
        if len(node_ids) < 2 or len(set(node_ids)) != len(node_ids):
            raise V8SchemaError(f"shared_models[{index}].node_ids必须含至少2个不同节点")
        if any(node_id not in by_id for node_id in node_ids):
            raise V8SchemaError(f"shared_models[{index}]引用不存在节点")
        shared_models.append({
            "id": shared_id,
            "description": _text(shared["description"], f"shared_models[{index}].description", 1200),
            "node_ids": node_ids,
            "basis": _text(shared["basis"], f"shared_models[{index}].basis", 1200),
        })

    indegree = {node_id: 0 for node_id in by_id}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in by_id}
    for edge in edges:
        outgoing[edge["from"]].append(edge["to"])
        indegree[edge["to"]] += 1
    queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
    distance = {node_id: 0 for node_id in by_id}
    visited = 0
    while queue:
        source = queue.popleft()
        visited += 1
        for target in outgoing[source]:
            distance[target] = max(distance[target], distance[source] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if visited != len(nodes):
        raise V8SchemaError("task_graph必须是有向无环图")

    metrics = {
        "node_count": len(nodes),
        "dependency_edge_count": len(edges),
        "shared_model_count": len(shared_models),
        "longest_dependency_path_edges": max(distance.values(), default=0),
        "cross_subquestion_edge_count": sum(
            by_id[edge["from"]]["subquestion"] != by_id[edge["to"]]["subquestion"]
            for edge in edges
        ),
        "node_kind_counts": dict(sorted(Counter(node["kind"] for node in nodes).items())),
        "edge_type_counts": dict(sorted(Counter(edge["type"] for edge in edges).items())),
    }
    return {"nodes": nodes, "edges": edges, "shared_models": shared_models}, metrics


def _validate_audit(value: Any) -> dict[str, str]:
    obj = _mapping(value, "audit_attributes")
    _exact(obj, AUDIT_FIELDS, "audit_attributes")
    result: dict[str, str] = {}
    for field, allowed in AUDIT_ENUMS.items():
        if obj[field] not in allowed:
            raise V8SchemaError(f"audit_attributes.{field}枚举非法")
        result[field] = obj[field]
    return result


def validate_and_prepare_v8_result(value: Any) -> dict[str, Any]:
    obj = _mapping(value, "v8_rating")
    _exact(obj, TOP_FIELDS, "v8_rating")
    assessment = _validate_assessment(obj["input_assessment"])
    can_rate = assessment["can_rate"]
    graph, metrics = _validate_graph(obj["task_graph"])
    decisive_reason = _text(obj["decisive_reason"], "decisive_reason", 1600)

    if not can_rate:
        if assessment["stem_readability"] != "关键缺失无法定档":
            raise V8SchemaError("can_rate=false时stem_readability必须为关键缺失无法定档")
        if not assessment["missing_information"]:
            raise V8SchemaError("can_rate=false时必须列出missing_information")
        for field in ("rated_target", "primary_dimensions", "audit_attributes", "boundary_review", "difficulty_level"):
            if obj[field] is not None:
                raise V8SchemaError(f"不可定档时{field}必须为null")
        if graph["nodes"] or graph["edges"] or graph["shared_models"]:
            raise V8SchemaError("不可定档时task_graph必须为空")
        raise UnrateableInputError("题干关键缺失，拒绝猜测档位")

    if assessment["stem_readability"] == "关键缺失无法定档":
        raise V8SchemaError("can_rate=true与关键缺失无法定档矛盾")
    level = obj["difficulty_level"]
    if level not in LEVELS:
        raise V8SchemaError("difficulty_level枚举非法")
    if not graph["nodes"]:
        raise V8SchemaError("可定档题至少需要一个任务节点")
    target = _validate_target(obj["rated_target"])
    dimensions = _validate_dimensions(obj["primary_dimensions"])
    audit = _validate_audit(obj["audit_attributes"])
    boundary = _mapping(obj["boundary_review"], "boundary_review")
    _exact(boundary, BOUNDARY_FIELDS, "boundary_review")
    lower, upper = BOUNDARY_NEIGHBORS[level]
    return {
        "input_assessment": assessment,
        "rated_target": target,
        "primary_dimensions": dimensions,
        "task_graph": graph,
        "audit_attributes": audit,
        "boundary_review": {
            "lower_level": lower,
            "why_not_lower": _text(boundary["why_not_lower"], "why_not_lower", 1400),
            "upper_level": upper,
            "why_not_higher": _text(boundary["why_not_higher"], "why_not_higher", 1400),
        },
        "decisive_reason": decisive_reason,
        "difficulty_level": level,
        "difficulty_level_raw": level,
        "postprocess_original_level": level,
        "postprocess_final_level": level,
        "derived_taskgraph_metrics": metrics,
        "feature_schema_version": "chemistry_v8_full_taskgraph_v1",
        "schema_validation_passed": True,
        "automatic_level_change_applied": False,
        "postprocess_actions": [],
        "postprocess_trace": [],
    }


def apply_v8_observe_only_postprocess(prepared: Mapping[str, Any]) -> dict[str, Any]:
    """Audit graph/level tensions while preserving the frozen model level."""
    result = copy.deepcopy(dict(prepared))
    level = result["postprocess_original_level"]
    metrics = result["derived_taskgraph_metrics"]
    flags: list[dict[str, str]] = []
    longest = metrics["longest_dependency_path_edges"]
    edges = metrics["dependency_edge_count"]
    nodes = metrics["node_count"]
    cross_edges = metrics["cross_subquestion_edge_count"]
    shared = metrics["shared_model_count"]
    if level == "送分题" and (longest >= 2 or cross_edges >= 1):
        flags.append({
            "code": "gift_with_dependency_tension",
            "reason": "送分题输出存在连续或跨问依赖，需复核任务图漏合并或档位低估。",
        })
    if LEVEL_RANK[level] >= 3 and edges == 0 and shared == 0 and nodes <= 1:
        flags.append({
            "code": "medium_plus_low_structure_tension",
            "reason": "中等及以上档位只有单节点、无依赖、无共享模型，需复核定档或任务图。",
        })
    if level == "拔高题" and edges == 0:
        flags.append({
            "code": "hard_without_decisive_edge_tension",
            "reason": "拔高题未输出任何会改变后续路径的依赖边。",
        })
    if level == "压轴题" and (edges < 2 or nodes <= 2):
        flags.append({
            "code": "final_without_coupling_tension",
            "reason": "压轴题任务图缺少至少两条关系或只有两个节点，需复核耦合是否充分。",
        })
    result["difficulty_level"] = level
    result["postprocess_final_level"] = level
    result["automatic_level_change_applied"] = False
    result["postprocess_actions"] = []
    result["postprocess_trace"] = []
    result["postprocess_profile"] = "v8_full_taskgraph_observe_only_v1"
    result["v8_postprocess_compatibility"] = "compatible_via_v8_schema_not_core12"
    result["v8_audit_flags"] = flags
    result["v8_audit_flag_count"] = len(flags)
    return result
