"""Audit-only structural checks for the chemistry A2-full experiment.

Physics postprocessing is useful where it relies on narrow, observable joint
conditions. For the first A2-full run these checks only surface candidates for
manual review. They deliberately never change a difficulty level.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping


LEVEL_RANK = {"送分题": 1, "基础题": 2, "中等题": 3, "拔高题": 4, "压轴题": 5}


def _flag(code: str, reason: str, evidence: list[str]) -> dict[str, Any]:
    return {"code": code, "reason": reason, "evidence": evidence}


def apply_a2_full_audit(
    rating: Mapping[str, Any],
    reconstruction: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach narrow structural tension flags without changing the level."""

    result = copy.deepcopy(dict(rating))
    level = str(result.get("difficulty_level", ""))
    rank = LEVEL_RANK.get(level, 0)
    graph = reconstruction.get("task_graph", {}) if isinstance(reconstruction, Mapping) else {}
    metrics = reconstruction.get("derived_graph_metrics", {}) if isinstance(reconstruction, Mapping) else {}
    audit = reconstruction.get("audit_attributes", {}) if isinstance(reconstruction, Mapping) else {}
    dimensions = result.get("rating_dimensions", {})
    review = result.get("task_graph_review", {})
    anchor = result.get("special_anchor", {})

    node_count = int(metrics.get("node_count", 0) or 0)
    decision_nodes = int(metrics.get("decision_node_count", 0) or 0)
    edge_count = int(metrics.get("dependency_edge_count", 0) or 0)
    shared_count = int(metrics.get("shared_model_count", 0) or 0)
    cross_edges = int(metrics.get("cross_subquestion_edge_count", 0) or 0)
    flags: list[dict[str, Any]] = []

    low_audit_structure = (
        audit.get("question_relation") in ("无多问", "多问相互独立")
        and audit.get("reaction_structure") in ("无反应任务", "单一直接反应", "多个并列反应")
        and audit.get("experiment_structure") in ("无实验任务", "基础操作或读数")
        and audit.get("graph_table_structure") in ("无图表任务", "直接读数")
        and audit.get("calculation_structure") in ("无定量任务", "已给关系直接代入")
        and audit.get("information_transfer") in ("课内直接原型", "给定新信息直接应用")
    )
    low_rating_dimensions = (
        dimensions.get("dependency_demand") in ("直接识别", "单一显性应用")
        and dimensions.get("model_demand") in ("无需模型", "单一显性规则")
        and dimensions.get("evidence_constraint_demand") in ("无证据约束任务", "单一显性条件")
        and dimensions.get("quantitative_demand") in ("无定量", "直接代入")
        and dimensions.get("transfer_demand") in ("课内原型", "给定信息直接用")
    )
    if (
        rank >= 3
        and decision_nodes <= 1
        and edge_count == 0
        and shared_count == 0
        and low_audit_structure
        and low_rating_dimensions
    ):
        flags.append(_flag(
            "low_structure_overrating_candidate",
            "档位达到中等或以上，但两个阶段均描述为单节点、无依赖、无共享模型的低结构任务。",
            [
                f"decision_node_count={decision_nodes}",
                "dependency_edge_count=0",
                "shared_model_count=0",
                "重建审计与定档维度均为低结构",
            ],
        ))

    if (
        level == "拔高题"
        and edge_count == 0
        and dimensions.get("dependency_demand") != "决定性高阶任务边"
    ):
        flags.append(_flag(
            "hard_without_decisive_edge_candidate",
            "拔高档没有任务图依赖边，定档维度也未声明决定性高阶任务边。",
            [f"node_count={node_count}", "dependency_edge_count=0"],
        ))

    fixed_group_anchor = anchor.get("type") == "固定基团教师锚点"
    if level == "压轴题" and not fixed_group_anchor and edge_count < 2:
        flags.append(_flag(
            "final_without_coupling_candidate",
            "非特殊锚点压轴题不足两条真实依赖边，需人工核查是否只有单一卡点或任务图漏边。",
            [
                f"dependency_edge_count={edge_count}",
                f"shared_model_count={shared_count}",
                f"cross_subquestion_edge_count={cross_edges}",
            ],
        ))

    review_status = str(review.get("status", ""))
    if review_status in ("重建不足", "重建过度"):
        flags.append(_flag(
            "task_graph_reconstruction_disagreement",
            "定档阶段明确认为第一阶段任务图存在重建争议。",
            [review_status, str(review.get("explanation", ""))],
        ))

    if graph and decision_nodes > node_count:
        # Defensive invariant; the strict schema should make this unreachable.
        flags.append(_flag(
            "invalid_graph_metric_candidate",
            "决策节点数超过总节点数，审计指标异常。",
            [f"decision_node_count={decision_nodes}", f"node_count={node_count}"],
        ))

    result["a2_full_audit_flags"] = flags
    result["a2_full_audit_flag_count"] = len(flags)
    result["a2_full_audit_only"] = True
    result["automatic_level_change_applied"] = False
    result["postprocess_final_level"] = result.get("postprocess_original_level", level)
    result["difficulty_level"] = result.get("postprocess_original_level", level)
    result["postprocess_actions"] = []
    return result
