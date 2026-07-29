"""Strict contracts for the chemistry two-pass task-graph A2 experiment.

Pass 1 reconstructs the complete question and a minimal sufficient task graph
without seeing or outputting a difficulty level. Pass 2 receives the validated
pass-1 object and assigns a teacher-facing level. Graph metrics and neighboring
level names are derived here for audit only; they never change the model level.
"""

from __future__ import annotations

import copy
import re
from collections import Counter, deque
from typing import Any, Mapping


LEVELS = ("送分题", "基础题", "中等题", "拔高题", "压轴题")
BOUNDARY_NEIGHBORS = {
    "送分题": (None, "基础题"),
    "基础题": ("送分题", "中等题"),
    "中等题": ("基础题", "拔高题"),
    "拔高题": ("中等题", "压轴题"),
    "压轴题": ("拔高题", None),
}

RECONSTRUCTION_FIELDS = {
    "input_assessment", "reconstructed_question", "rated_target",
    "task_graph", "audit_attributes",
}
INPUT_FIELDS = {
    "can_reconstruct", "stem_readability", "solution_readability",
    "missing_information", "conflict_description",
}
RECONSTRUCTED_FIELDS = {"complete_task_summary", "subquestions", "source_resolution"}
RATED_TARGET_FIELDS = {"scope", "task", "included_prerequisites", "selection_reason"}
GRAPH_FIELDS = {"nodes", "edges", "shared_models"}
NODE_FIELDS = {
    "id", "subquestion", "primary_kind", "tags", "counts_as_decision",
    "action", "output", "source_evidence",
}
EDGE_FIELDS = {"from", "to", "type", "basis"}
SHARED_MODEL_FIELDS = {"id", "description", "node_ids", "basis"}
AUDIT_FIELDS = {
    "question_relation", "visual_role", "reaction_structure",
    "experiment_structure", "graph_table_structure", "calculation_structure",
    "information_transfer",
}

RATING_FIELDS = {
    "difficulty_level", "rating_dimensions", "boundary_review",
    "decisive_reason", "task_graph_review", "special_anchor",
}
RATING_DIMENSION_FIELDS = {
    "dependency_demand", "model_demand", "evidence_constraint_demand",
    "quantitative_demand", "transfer_demand",
}
BOUNDARY_FIELDS = {"why_not_lower", "why_not_higher"}
GRAPH_REVIEW_FIELDS = {"status", "explanation"}
SPECIAL_ANCHOR_FIELDS = {"type", "evidence"}

STEM_READABILITY = ("完整", "局部缺失但可重建", "关键缺失无法重建")
SOLUTION_READABILITY = (
    "完整", "局部缺失但不影响任务重建", "未提供但题面可独立重建",
    "关键缺失但题面仍可重建", "与题干存在冲突",
)
PRIMARY_KINDS = (
    "calculation", "experiment", "evidence", "graph", "reaction",
    "constraint", "modeling", "rule_application", "recall",
)
NODE_TAGS = (
    "calculation", "experiment", "evidence", "graph", "reaction",
    "constraint", "modeling", "rule_application", "recall",
    "representation_conversion", "information_transfer",
)
EDGE_TYPES = ("result_reuse", "condition_dependency", "evidence_dependency")

AUDIT_ENUMS = {
    "question_relation": (
        "无多问", "多问相互独立", "多问共享模型但无结果依赖",
        "多问存在结果或任务链依赖",
    ),
    "visual_role": ("无实质视觉信息", "直接读取或识图", "提供局部关系", "决定阶段或模型"),
    "reaction_structure": (
        "无反应任务", "单一直接反应", "多个并列反应", "简单连续反应",
        "先后、过量或竞争改变路径", "多反应耦合模型",
    ),
    "experiment_structure": (
        "无实验任务", "基础操作或读数", "常规实验闭环",
        "方案设计、评价或干扰排除", "多阶段实验与定量误差",
    ),
    "graph_table_structure": (
        "无图表任务", "直接读数", "比较归纳", "阶段对应",
        "拐点、平台或分段反推", "多图表耦合",
    ),
    "calculation_structure": (
        "无定量任务", "已给关系直接代入", "常规单模型计算",
        "单一高阶守恒或多反应计算", "多重守恒、差量、联立或分类",
    ),
    "information_transfer": (
        "课内直接原型", "给定新信息直接应用", "迁移后建立关系", "完全陌生模型现场建立",
    ),
}

RATING_DIMENSION_ENUMS = {
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


class TwoPassSchemaError(ValueError):
    """The model output violates an A2 contract."""


class UnreconstructableInputError(TwoPassSchemaError):
    """The stem lacks information required to reconstruct the student task."""


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TwoPassSchemaError(f"{name}必须是JSON对象")
    return dict(value)


def _exact(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        raise TwoPassSchemaError(f"{name}字段不匹配: missing={missing}, extra={extra}")


def _text(value: Any, name: str, limit: int = 1600, *, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and not allow_empty:
        raise TwoPassSchemaError(f"{name}不能为空")
    if len(text) > limit:
        raise TwoPassSchemaError(f"{name}过长: {len(text)} > {limit}")
    return text


def _text_list(value: Any, name: str, maximum: int, limit: int = 500) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise TwoPassSchemaError(f"{name}必须是最多{maximum}项的数组")
    return [_text(item, f"{name}[{i}]", limit) for i, item in enumerate(value)]


def _nullable_object(value: Any, name: str) -> None:
    if value is not None:
        raise TwoPassSchemaError(f"不可重建时{name}必须为null")


def _validate_input(value: Any) -> dict[str, Any]:
    obj = _mapping(value, "input_assessment")
    _exact(obj, INPUT_FIELDS, "input_assessment")
    if not isinstance(obj["can_reconstruct"], bool):
        raise TwoPassSchemaError("can_reconstruct必须是布尔值")
    if obj["stem_readability"] not in STEM_READABILITY:
        raise TwoPassSchemaError("stem_readability枚举非法")
    if obj["solution_readability"] not in SOLUTION_READABILITY:
        raise TwoPassSchemaError("solution_readability枚举非法")
    return {
        "can_reconstruct": obj["can_reconstruct"],
        "stem_readability": obj["stem_readability"],
        "solution_readability": obj["solution_readability"],
        "missing_information": _text_list(obj["missing_information"], "missing_information", 10, 300),
        "conflict_description": _text(obj["conflict_description"], "conflict_description", 600),
    }


def _validate_reconstructed(value: Any) -> dict[str, Any]:
    obj = _mapping(value, "reconstructed_question")
    _exact(obj, RECONSTRUCTED_FIELDS, "reconstructed_question")
    return {
        "complete_task_summary": _text(obj["complete_task_summary"], "complete_task_summary", 2400),
        "subquestions": _text_list(obj["subquestions"], "subquestions", 30, 800),
        "source_resolution": _text(obj["source_resolution"], "source_resolution", 1000),
    }


def _validate_target(value: Any) -> dict[str, Any]:
    obj = _mapping(value, "rated_target")
    _exact(obj, RATED_TARGET_FIELDS, "rated_target")
    return {
        "scope": _text(obj["scope"], "rated_target.scope", 300),
        "task": _text(obj["task"], "rated_target.task", 900),
        "included_prerequisites": _text_list(obj["included_prerequisites"], "included_prerequisites", 15, 500),
        "selection_reason": _text(obj["selection_reason"], "rated_target.selection_reason", 1000),
    }


def _validate_graph(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    graph = _mapping(value, "task_graph")
    _exact(graph, GRAPH_FIELDS, "task_graph")
    if not isinstance(graph["nodes"], list) or not 1 <= len(graph["nodes"]) <= 30:
        raise TwoPassSchemaError("nodes必须是1到30项的数组")
    nodes: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for i, raw in enumerate(graph["nodes"]):
        node = _mapping(raw, f"nodes[{i}]")
        _exact(node, NODE_FIELDS, f"nodes[{i}]")
        node_id = _text(node["id"], f"nodes[{i}].id", 20)
        if not re.fullmatch(r"N[1-9]\d*", node_id) or node_id in by_id:
            raise TwoPassSchemaError(f"非法或重复节点ID: {node_id}")
        kind = node["primary_kind"]
        if kind not in PRIMARY_KINDS:
            raise TwoPassSchemaError(f"节点{node_id}的primary_kind非法")
        tags = _text_list(node["tags"], f"nodes[{i}].tags", 6, 40)
        if len(tags) != len(set(tags)) or any(tag not in NODE_TAGS for tag in tags):
            raise TwoPassSchemaError(f"节点{node_id}的tags非法或重复")
        if kind in tags:
            raise TwoPassSchemaError(f"节点{node_id}的tags不得重复primary_kind")
        if not isinstance(node["counts_as_decision"], bool):
            raise TwoPassSchemaError(f"节点{node_id}.counts_as_decision必须是布尔值")
        normalized = {
            "id": node_id,
            "subquestion": _text(node["subquestion"], f"nodes[{i}].subquestion", 200),
            "primary_kind": kind,
            "tags": tags,
            "counts_as_decision": node["counts_as_decision"],
            "action": _text(node["action"], f"nodes[{i}].action", 800),
            "output": _text(node["output"], f"nodes[{i}].output", 800),
            "source_evidence": _text(node["source_evidence"], f"nodes[{i}].source_evidence", 900),
        }
        by_id[node_id] = normalized
        nodes.append(normalized)

    if not isinstance(graph["edges"], list) or len(graph["edges"]) > 60:
        raise TwoPassSchemaError("edges必须是最多60项的数组")
    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for i, raw in enumerate(graph["edges"]):
        edge = _mapping(raw, f"edges[{i}]")
        _exact(edge, EDGE_FIELDS, f"edges[{i}]")
        source, target, edge_type = edge["from"], edge["to"], edge["type"]
        if source not in by_id or target not in by_id or source == target:
            raise TwoPassSchemaError(f"edges[{i}]引用非法节点")
        if edge_type not in EDGE_TYPES:
            raise TwoPassSchemaError(f"edges[{i}].type非法")
        key = (source, target, edge_type)
        if key in seen_edges:
            raise TwoPassSchemaError(f"重复边: {key}")
        seen_edges.add(key)
        edges.append({"from": source, "to": target, "type": edge_type, "basis": _text(edge["basis"], f"edges[{i}].basis", 800)})

    if not isinstance(graph["shared_models"], list) or len(graph["shared_models"]) > 15:
        raise TwoPassSchemaError("shared_models必须是最多15项的数组")
    shared_models: list[dict[str, Any]] = []
    seen_models: set[str] = set()
    for i, raw in enumerate(graph["shared_models"]):
        model = _mapping(raw, f"shared_models[{i}]")
        _exact(model, SHARED_MODEL_FIELDS, f"shared_models[{i}]")
        model_id = _text(model["id"], f"shared_models[{i}].id", 20)
        if not re.fullmatch(r"M[1-9]\d*", model_id) or model_id in seen_models:
            raise TwoPassSchemaError(f"非法或重复共享模型ID: {model_id}")
        node_ids = _text_list(model["node_ids"], f"shared_models[{i}].node_ids", 30, 20)
        if len(node_ids) < 2 or len(node_ids) != len(set(node_ids)) or any(n not in by_id for n in node_ids):
            raise TwoPassSchemaError(f"共享模型{model_id}必须引用至少两个不同的有效节点")
        seen_models.add(model_id)
        shared_models.append({
            "id": model_id,
            "description": _text(model["description"], f"shared_models[{i}].description", 900),
            "node_ids": node_ids,
            "basis": _text(model["basis"], f"shared_models[{i}].basis", 900),
        })

    indegree = {node_id: 0 for node_id in by_id}
    adjacency = {node_id: [] for node_id in by_id}
    for edge in edges:
        adjacency[edge["from"]].append(edge["to"])
        indegree[edge["to"]] += 1
    queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while queue:
        node_id = queue.popleft()
        order.append(node_id)
        for child in adjacency[node_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(order) != len(by_id):
        raise TwoPassSchemaError("task_graph包含循环依赖")

    counted = {node_id for node_id, node in by_id.items() if node["counts_as_decision"]}
    if not counted:
        raise TwoPassSchemaError("任务图至少需要一个counts_as_decision=true的节点")
    # The metric name is explicit: count decision nodes, not dependency edges.
    # A stand-alone counted node therefore has path length 1. Shared-model
    # membership never enters adjacency and cannot inflate this path.
    distance = {node_id: (1 if node_id in counted else 0) for node_id in by_id}
    for source in order:
        for target in adjacency[source]:
            candidate = distance[source] + (1 if target in counted else 0)
            distance[target] = max(distance[target], candidate)
    cross_edges = sum(by_id[e["from"]]["subquestion"] != by_id[e["to"]]["subquestion"] for e in edges)
    metrics = {
        "node_count": len(nodes),
        "decision_node_count": len(counted),
        "dependency_edge_count": len(edges),
        "shared_model_count": len(shared_models),
        "longest_decision_path_nodes": max(distance.values(), default=0),
        "cross_subquestion_edge_count": cross_edges,
        "edge_type_counts": dict(Counter(e["type"] for e in edges)),
        "primary_kind_counts": dict(Counter(n["primary_kind"] for n in nodes)),
    }
    return {"nodes": nodes, "edges": edges, "shared_models": shared_models}, metrics


def _validate_audit(value: Any) -> dict[str, str]:
    obj = _mapping(value, "audit_attributes")
    _exact(obj, AUDIT_FIELDS, "audit_attributes")
    result: dict[str, str] = {}
    for field, allowed in AUDIT_ENUMS.items():
        if obj[field] not in allowed:
            raise TwoPassSchemaError(f"audit_attributes.{field}枚举非法")
        result[field] = obj[field]
    return result


def validate_reconstruction(value: Any) -> dict[str, Any]:
    obj = _mapping(value, "task_reconstruction")
    _exact(obj, RECONSTRUCTION_FIELDS, "task_reconstruction")
    assessment = _validate_input(obj["input_assessment"])
    if not assessment["can_reconstruct"]:
        if assessment["stem_readability"] != "关键缺失无法重建":
            raise TwoPassSchemaError("can_reconstruct=false时题干必须为关键缺失无法重建")
        for field in ("reconstructed_question", "rated_target", "audit_attributes"):
            _nullable_object(obj[field], field)
        graph = _mapping(obj["task_graph"], "task_graph")
        _exact(graph, GRAPH_FIELDS, "task_graph")
        if graph != {"nodes": [], "edges": [], "shared_models": []}:
            raise TwoPassSchemaError("不可重建时task_graph必须全部为空数组")
        raise UnreconstructableInputError("题干关键缺失，拒绝进入定档阶段")
    if assessment["stem_readability"] == "关键缺失无法重建":
        raise TwoPassSchemaError("can_reconstruct=true与题干关键缺失矛盾")
    graph, metrics = _validate_graph(obj["task_graph"])
    return {
        "input_assessment": assessment,
        "reconstructed_question": _validate_reconstructed(obj["reconstructed_question"]),
        "rated_target": _validate_target(obj["rated_target"]),
        "task_graph": graph,
        "audit_attributes": _validate_audit(obj["audit_attributes"]),
        "derived_graph_metrics": metrics,
    }


def validate_rating(value: Any, reconstruction: Mapping[str, Any]) -> dict[str, Any]:
    obj = _mapping(value, "difficulty_rating")
    _exact(obj, RATING_FIELDS, "difficulty_rating")
    level = obj["difficulty_level"]
    if level not in LEVELS:
        raise TwoPassSchemaError("difficulty_level枚举非法")
    dims = _mapping(obj["rating_dimensions"], "rating_dimensions")
    _exact(dims, RATING_DIMENSION_FIELDS, "rating_dimensions")
    normalized_dims: dict[str, str] = {}
    for field, allowed in RATING_DIMENSION_ENUMS.items():
        if dims[field] not in allowed:
            raise TwoPassSchemaError(f"rating_dimensions.{field}枚举非法")
        normalized_dims[field] = dims[field]
    boundary = _mapping(obj["boundary_review"], "boundary_review")
    _exact(boundary, BOUNDARY_FIELDS, "boundary_review")
    review = _mapping(obj["task_graph_review"], "task_graph_review")
    _exact(review, GRAPH_REVIEW_FIELDS, "task_graph_review")
    if review["status"] not in ("一致", "重建不足", "重建过度"):
        raise TwoPassSchemaError("task_graph_review.status枚举非法")
    anchor = _mapping(obj["special_anchor"], "special_anchor")
    _exact(anchor, SPECIAL_ANCHOR_FIELDS, "special_anchor")
    if anchor["type"] not in ("无", "固定基团教师锚点"):
        raise TwoPassSchemaError("special_anchor.type枚举非法")
    if not reconstruction.get("task_graph"):
        raise TwoPassSchemaError("定档阶段缺少已校验任务图")
    lower, upper = BOUNDARY_NEIGHBORS[level]
    return {
        "difficulty_level": level,
        "rating_dimensions": normalized_dims,
        "boundary_review": {
            "lower_level": lower,
            "why_not_lower": _text(boundary["why_not_lower"], "why_not_lower", 1200),
            "upper_level": upper,
            "why_not_higher": _text(boundary["why_not_higher"], "why_not_higher", 1200),
        },
        "decisive_reason": _text(obj["decisive_reason"], "decisive_reason", 1600),
        "task_graph_review": {
            "status": review["status"],
            "explanation": _text(review["explanation"], "task_graph_review.explanation", 1200),
        },
        "special_anchor": {
            "type": anchor["type"],
            "evidence": _text(anchor["evidence"], "special_anchor.evidence", 1000),
        },
        "postprocess_original_level": level,
        "postprocess_final_level": level,
        "postprocess_actions": [],
        "automatic_level_change_applied": False,
        "audit_only": True,
    }


def prepare_two_pass_result(reconstruction_raw: Any, rating_raw: Any) -> dict[str, Any]:
    reconstruction = validate_reconstruction(reconstruction_raw)
    rating = validate_rating(rating_raw, reconstruction)
    return {
        "task_reconstruction": copy.deepcopy(reconstruction),
        "difficulty_rating": copy.deepcopy(rating),
    }
