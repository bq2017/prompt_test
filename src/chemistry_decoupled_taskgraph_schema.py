"""Strict schema for the decoupled chemistry task-graph rating contract.

The model makes the primary difficulty decision from teacher-facing semantic
standards, then emits a minimal task graph that records the evidence used.
This module validates the graph and derives only deterministic graph metrics.
It deliberately does not reconstruct a difficulty level from model-generated
semantic features.
"""

from __future__ import annotations

import copy
import re
from collections import Counter, deque
from typing import Any, Mapping


LEVELS = ("送分题", "基础题", "中等题", "拔高题", "压轴题")

TOP_LEVEL_FIELDS = {
    "input_assessment",
    "primary_dimensions",
    "task_graph",
    "boundary_review",
    "decisive_reason",
    "difficulty_level",
}

INPUT_FIELDS = {
    "can_rate",
    "stem_readability",
    "solution_readability",
    "missing_information",
    "conflict_description",
}

PRIMARY_DIMENSION_FIELDS = {
    "dependency_structure",
    "decisive_transformation",
    "model_requirement",
    "evidence_constraints",
    "coupling_structure",
}

TASK_GRAPH_FIELDS = {"nodes", "edges"}
NODE_FIELDS = {"id", "subquestion", "kind", "action", "output", "source_evidence"}
EDGE_FIELDS = {"from", "to", "type", "basis"}
BOUNDARY_FIELDS = {"lower_level", "why_not_lower", "upper_level", "why_not_higher"}

STEM_READABILITY = ("完整", "局部缺失但可定档", "关键缺失无法定档")
SOLUTION_READABILITY = ("完整", "局部缺失但可定档", "未提供但可定档", "关键缺失无法定档")

NODE_KINDS = (
    "recall",
    "rule_application",
    "reaction",
    "experiment",
    "evidence",
    "graph",
    "calculation",
    "constraint",
    "modeling",
    "conclusion",
)

EDGE_TYPES = (
    "result_reuse",
    "condition_dependency",
    "model_dependency",
    "evidence_dependency",
)

BOUNDARY_NEIGHBORS = {
    "送分题": ("无（最低档）", "基础题"),
    "基础题": ("送分题", "中等题"),
    "中等题": ("基础题", "拔高题"),
    "拔高题": ("中等题", "压轴题"),
    "压轴题": ("拔高题", "无（最高档）"),
}


class DecoupledSchemaError(ValueError):
    """Raised when the model response violates the production contract."""


class UnrateableInputError(DecoupledSchemaError):
    """Raised when the model correctly reports that key input is unreadable."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DecoupledSchemaError(f"{name}必须是JSON对象")
    return dict(value)


def _require_exact_fields(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise DecoupledSchemaError(f"{name}字段不匹配: missing={missing}, extra={extra}")


def _require_nonempty_text(value: Any, name: str, *, max_length: int = 1200) -> str:
    text = _text(value)
    if not text:
        raise DecoupledSchemaError(f"{name}不能为空")
    if len(text) > max_length:
        raise DecoupledSchemaError(f"{name}过长: {len(text)} > {max_length}")
    return text


def _validate_input_assessment(value: Any) -> dict[str, Any]:
    assessment = _require_mapping(value, "input_assessment")
    _require_exact_fields(assessment, INPUT_FIELDS, "input_assessment")

    if not isinstance(assessment["can_rate"], bool):
        raise DecoupledSchemaError("input_assessment.can_rate必须是布尔值")
    if assessment["stem_readability"] not in STEM_READABILITY:
        raise DecoupledSchemaError("stem_readability枚举非法")
    if assessment["solution_readability"] not in SOLUTION_READABILITY:
        raise DecoupledSchemaError("solution_readability枚举非法")
    if not isinstance(assessment["missing_information"], list):
        raise DecoupledSchemaError("missing_information必须是数组")
    if len(assessment["missing_information"]) > 10:
        raise DecoupledSchemaError("missing_information最多10项")

    missing = [
        _require_nonempty_text(item, f"missing_information[{index}]", max_length=300)
        for index, item in enumerate(assessment["missing_information"])
    ]
    conflict = _require_nonempty_text(
        assessment["conflict_description"],
        "input_assessment.conflict_description",
        max_length=600,
    )
    return {
        "can_rate": assessment["can_rate"],
        "stem_readability": assessment["stem_readability"],
        "solution_readability": assessment["solution_readability"],
        "missing_information": missing,
        "conflict_description": conflict,
    }


def _validate_primary_dimensions(value: Any) -> dict[str, str]:
    dimensions = _require_mapping(value, "primary_dimensions")
    _require_exact_fields(dimensions, PRIMARY_DIMENSION_FIELDS, "primary_dimensions")
    return {
        field: _require_nonempty_text(dimensions[field], f"primary_dimensions.{field}")
        for field in sorted(PRIMARY_DIMENSION_FIELDS)
    }


def _validate_nodes(value: Any) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    if not isinstance(value, list):
        raise DecoupledSchemaError("task_graph.nodes必须是数组")
    if len(value) > 20:
        raise DecoupledSchemaError("task_graph.nodes最多20项")

    nodes: list[dict[str, str]] = []
    by_id: dict[str, dict[str, str]] = {}
    for index, raw in enumerate(value):
        node = _require_mapping(raw, f"task_graph.nodes[{index}]")
        _require_exact_fields(node, NODE_FIELDS, f"task_graph.nodes[{index}]")
        node_id = _require_nonempty_text(node["id"], f"nodes[{index}].id", max_length=20)
        if not re.fullmatch(r"N[1-9]\d*", node_id):
            raise DecoupledSchemaError(f"节点ID必须形如N1: {node_id!r}")
        if node_id in by_id:
            raise DecoupledSchemaError(f"节点ID重复: {node_id}")
        kind = _text(node["kind"])
        if kind not in NODE_KINDS:
            raise DecoupledSchemaError(f"节点kind非法: {kind!r}")
        prepared = {
            "id": node_id,
            "subquestion": _require_nonempty_text(
                node["subquestion"], f"nodes[{index}].subquestion", max_length=100
            ),
            "kind": kind,
            "action": _require_nonempty_text(node["action"], f"nodes[{index}].action"),
            "output": _require_nonempty_text(node["output"], f"nodes[{index}].output"),
            "source_evidence": _require_nonempty_text(
                node["source_evidence"], f"nodes[{index}].source_evidence"
            ),
        }
        nodes.append(prepared)
        by_id[node_id] = prepared
    return nodes, by_id


def _validate_edges(value: Any, by_id: Mapping[str, Mapping[str, str]]) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise DecoupledSchemaError("task_graph.edges必须是数组")
    if len(value) > 60:
        raise DecoupledSchemaError("task_graph.edges最多60项")

    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(value):
        edge = _require_mapping(raw, f"task_graph.edges[{index}]")
        _require_exact_fields(edge, EDGE_FIELDS, f"task_graph.edges[{index}]")
        source = _text(edge["from"])
        target = _text(edge["to"])
        edge_type = _text(edge["type"])
        if source not in by_id or target not in by_id:
            raise DecoupledSchemaError(f"边引用未知节点: {source!r}->{target!r}")
        if source == target:
            raise DecoupledSchemaError(f"任务图不允许自环: {source}")
        if edge_type not in EDGE_TYPES:
            raise DecoupledSchemaError(f"边type非法: {edge_type!r}")
        key = (source, target, edge_type)
        if key in seen:
            raise DecoupledSchemaError(f"重复依赖边: {key}")
        seen.add(key)
        edges.append(
            {
                "from": source,
                "to": target,
                "type": edge_type,
                "basis": _require_nonempty_text(edge["basis"], f"edges[{index}].basis"),
            }
        )
    return edges


def _graph_metrics(
    nodes: list[dict[str, str]],
    edges: list[dict[str, str]],
) -> dict[str, Any]:
    node_ids = [node["id"] for node in nodes]
    indegree = {node_id: 0 for node_id in node_ids}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    edge_lookup: dict[tuple[str, str], list[str]] = {}
    for edge in edges:
        source = edge["from"]
        target = edge["to"]
        outgoing[source].append(target)
        indegree[target] += 1
        edge_lookup.setdefault((source, target), []).append(edge["type"])

    queue = deque(node_id for node_id in node_ids if indegree[node_id] == 0)
    distance = {node_id: 0 for node_id in node_ids}
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
        raise DecoupledSchemaError("task_graph必须是有向无环图，当前存在循环依赖")

    by_id = {node["id"]: node for node in nodes}
    cross_subquestion = sum(
        by_id[edge["from"]]["subquestion"] != by_id[edge["to"]]["subquestion"]
        for edge in edges
    )
    return {
        "node_count": len(nodes),
        "dependency_edge_count": len(edges),
        "longest_dependency_path_edges": max(distance.values(), default=0),
        "cross_subquestion_edge_count": cross_subquestion,
        "node_kind_counts": dict(sorted(Counter(node["kind"] for node in nodes).items())),
        "edge_type_counts": dict(sorted(Counter(edge["type"] for edge in edges).items())),
        "root_node_count": sum(
            not any(edge["to"] == node_id for edge in edges) for node_id in node_ids
        ),
        "leaf_node_count": sum(not outgoing[node_id] for node_id in node_ids),
    }


def _validate_task_graph(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    graph = _require_mapping(value, "task_graph")
    _require_exact_fields(graph, TASK_GRAPH_FIELDS, "task_graph")
    nodes, by_id = _validate_nodes(graph["nodes"])
    edges = _validate_edges(graph["edges"], by_id)
    metrics = _graph_metrics(nodes, edges)
    return {"nodes": nodes, "edges": edges}, metrics


def _validate_boundary(value: Any, level: str | None, can_rate: bool) -> dict[str, str]:
    boundary = _require_mapping(value, "boundary_review")
    _require_exact_fields(boundary, BOUNDARY_FIELDS, "boundary_review")
    prepared = {
        field: _require_nonempty_text(boundary[field], f"boundary_review.{field}")
        for field in sorted(BOUNDARY_FIELDS)
    }
    if can_rate:
        expected_lower, expected_upper = BOUNDARY_NEIGHBORS[level]
        if prepared["lower_level"] != expected_lower:
            raise DecoupledSchemaError(
                f"{level}的lower_level应为{expected_lower!r}，实际为{prepared['lower_level']!r}"
            )
        if prepared["upper_level"] != expected_upper:
            raise DecoupledSchemaError(
                f"{level}的upper_level应为{expected_upper!r}，实际为{prepared['upper_level']!r}"
            )
    else:
        if prepared["lower_level"] != "无法判断" or prepared["upper_level"] != "无法判断":
            raise DecoupledSchemaError("不可评级时lower_level和upper_level必须均为'无法判断'")
    return prepared


def validate_and_prepare_result(
    result: Mapping[str, Any],
    data: Mapping[str, Any] | None = None,
    *,
    allow_legacy: bool = False,
) -> dict[str, Any]:
    """Validate one model result and add deterministic graph audit metrics."""

    del data, allow_legacy
    prepared = _require_mapping(result, "模型输出")
    _require_exact_fields(prepared, TOP_LEVEL_FIELDS, "模型输出")

    assessment = _validate_input_assessment(prepared["input_assessment"])
    can_rate = assessment["can_rate"]
    raw_level = prepared["difficulty_level"]
    if can_rate:
        if raw_level not in LEVELS:
            raise DecoupledSchemaError(f"difficulty_level非法: {raw_level!r}")
        if (
            assessment["stem_readability"] == "关键缺失无法定档"
            or assessment["solution_readability"] == "关键缺失无法定档"
        ):
            raise DecoupledSchemaError("can_rate=true与'关键缺失无法定档'冲突")
    else:
        if raw_level is not None:
            raise DecoupledSchemaError("can_rate=false时difficulty_level必须为null")
        if not assessment["missing_information"]:
            raise DecoupledSchemaError("can_rate=false时必须列出missing_information")
        if (
            assessment["stem_readability"] != "关键缺失无法定档"
            and assessment["solution_readability"] != "关键缺失无法定档"
        ):
            raise DecoupledSchemaError("can_rate=false时必须标记至少一项关键缺失")

    dimensions = _validate_primary_dimensions(prepared["primary_dimensions"])
    graph, metrics = _validate_task_graph(prepared["task_graph"])
    if can_rate and not graph["nodes"]:
        raise DecoupledSchemaError("可评级题至少需要一个任务节点")
    if not can_rate and (graph["nodes"] or graph["edges"]):
        raise DecoupledSchemaError("不可评级题的任务图必须为空")

    boundary = _validate_boundary(prepared["boundary_review"], raw_level, can_rate)
    decisive_reason = _require_nonempty_text(prepared["decisive_reason"], "decisive_reason")

    if not can_rate:
        raise UnrateableInputError(
            "输入关键内容缺失，模型已正确拒绝猜档: "
            + "; ".join(assessment["missing_information"])
        )

    return {
        "input_assessment": assessment,
        "primary_dimensions": dimensions,
        "task_graph": graph,
        "boundary_review": boundary,
        "decisive_reason": decisive_reason,
        "difficulty_level": raw_level,
        "difficulty_level_raw": raw_level,
        "postprocess_original_level": raw_level,
        "derived_features": metrics,
        "feature_schema_version": "chemistry_decoupled_taskgraph_v1",
        "schema_validation_passed": True,
        "automatic_level_change_applied": False,
        "postprocess_actions": [],
        "postprocess_trace": [],
    }


def apply_observe_only_audit(prepared: Mapping[str, Any]) -> dict[str, Any]:
    """Record graph/label tensions without changing the model's primary level.

    No automatic rules are approved for the first experiment.  Candidate flags
    are diagnostic only and must be validated over repeated runs and reviewed
    labels before any adjacent-level rule is enabled.
    """

    result = copy.deepcopy(dict(prepared))
    level = result["difficulty_level"]
    metrics = result["derived_features"]
    flags: list[str] = []
    longest = metrics["longest_dependency_path_edges"]
    node_count = metrics["node_count"]
    cross_edges = metrics["cross_subquestion_edge_count"]
    if level == "送分题" and (longest >= 2 or cross_edges >= 1):
        flags.append("送分题与任务图中的连续/跨问依赖存在张力，需复核但不自动改档")
    if level == "压轴题" and node_count <= 2 and longest <= 1:
        flags.append("压轴题与当前最小任务图规模存在张力，可能是任务图漏抽或档位高估")
    result["postprocess_profile"] = "decoupled_observe_only_v1"
    result["rating_graph_tension_flags"] = flags
    return result
