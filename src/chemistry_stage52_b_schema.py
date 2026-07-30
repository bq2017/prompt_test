"""Strict Stage5.2-B audit schema and deterministic trigger."""

from __future__ import annotations

from typing import Any, Mapping


SCHEMA_VERSION = "stage52_b_audit_v1"
DELETE_EFFECTS = (
    "closure_breaks",
    "only_loses_independent_answer",
    "no_material_change",
)
AUDIT_FIELDS = (
    "constructed_representation",
    "representation_nonroutine",
    "operation_x",
    "operation_y",
    "different_causal_roles",
    "same_hardest_task",
    "independently_solvable",
    "answer_splicing",
    "mechanical_tail",
    "delete_x_effect",
    "delete_y_effect",
    "reason",
)
BOOLEAN_FIELDS = (
    "representation_nonroutine",
    "different_causal_roles",
    "same_hardest_task",
    "independently_solvable",
    "answer_splicing",
    "mechanical_tail",
)
TEXT_FIELDS = (
    "constructed_representation",
    "operation_x",
    "operation_y",
    "reason",
)


class Stage52BAuditError(ValueError):
    """Raised when an audit cannot be used for deterministic mapping."""


def validate_b_audit(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise Stage52BAuditError("B审计必须是JSON对象")
    missing = sorted(set(AUDIT_FIELDS) - set(raw))
    extra = sorted(set(raw) - set(AUDIT_FIELDS))
    if missing or extra:
        raise Stage52BAuditError(
            f"B审计字段不完整: missing={missing}, extra={extra}"
        )

    audit: dict[str, Any] = {}
    for field in BOOLEAN_FIELDS:
        value = raw[field]
        if type(value) is not bool:
            raise Stage52BAuditError(f"{field}必须是JSON布尔值")
        audit[field] = value
    for field in TEXT_FIELDS:
        value = str(raw[field] or "").strip()
        if not value:
            raise Stage52BAuditError(f"{field}不得为空")
        audit[field] = value
    for field in ("delete_x_effect", "delete_y_effect"):
        value = str(raw[field] or "").strip()
        if value not in DELETE_EFFECTS:
            raise Stage52BAuditError(
                f"{field}非法: {value!r}; allowed={DELETE_EFFECTS}"
            )
        audit[field] = value
    return {field: audit[field] for field in AUDIT_FIELDS}


def derive_composite_burden(audit: Mapping[str, Any]) -> bool:
    checked = validate_b_audit(audit)
    return bool(
        checked["representation_nonroutine"]
        and checked["different_causal_roles"]
        and checked["same_hardest_task"]
        and not checked["independently_solvable"]
        and not checked["answer_splicing"]
        and not checked["mechanical_tail"]
        and checked["delete_x_effect"] == "closure_breaks"
        and checked["delete_y_effect"] == "closure_breaks"
    )


def failed_gates(audit: Mapping[str, Any]) -> list[str]:
    checked = validate_b_audit(audit)
    gates = {
        "representation_nonroutine": checked["representation_nonroutine"],
        "different_causal_roles": checked["different_causal_roles"],
        "same_hardest_task": checked["same_hardest_task"],
        "not_independently_solvable": not checked["independently_solvable"],
        "not_answer_splicing": not checked["answer_splicing"],
        "not_mechanical_tail": not checked["mechanical_tail"],
        "delete_x_breaks_closure": checked["delete_x_effect"] == "closure_breaks",
        "delete_y_breaks_closure": checked["delete_y_effect"] == "closure_breaks",
    }
    return [name for name, passed in gates.items() if not passed]
