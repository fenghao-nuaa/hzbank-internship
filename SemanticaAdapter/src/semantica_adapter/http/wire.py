"""Explicit JSON wire mappings for the stable governance domain models."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, TypeAlias

from semantica_adapter.domain.models import (
    AgentProfile,
    ApprovalRecord,
    AuditRequest,
    AuditSession,
    AuditStatus,
    AuditTrace,
    DecisionRecord,
    EvidenceRef,
    PolicyExceptionRecord,
    RuleEvaluation,
)


JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)


def to_wire(value: Any) -> JSONValue:
    """Convert supported domain values into JSON-compatible primitives."""

    if isinstance(value, Enum):
        return to_wire(value.value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: to_wire(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_wire(item) for item in value]
    raise TypeError(f"unsupported wire value: {type(value).__name__}")


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _tuple_of_strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be an array of strings")
    return tuple(value)


def _datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO 8601 string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO 8601 string") from error
    if result.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return result


def agent_profile_from_wire(value: Any) -> AgentProfile:
    data = _mapping(value, "agent profile")
    return AgentProfile(
        agent_id=data["agent_id"],
        name=data["name"],
        purpose=data["purpose"],
        profile_version=data["profile_version"],
        rule_set_id=data["rule_set_id"],
        rule_set_version=data["rule_set_version"],
        ontology_id=data["ontology_id"],
        ontology_version=data["ontology_version"],
        allowed_source_types=_tuple_of_strings(
            data.get("allowed_source_types", ()), "allowed_source_types"
        ),
        approval_policy=data["approval_policy"],
        required_fields=_tuple_of_strings(data.get("required_fields", ()), "required_fields"),
        rules=dict(_mapping(data.get("rules", {}), "rules")),
        ontology=dict(_mapping(data.get("ontology", {}), "ontology")),
        metadata=dict(_mapping(data.get("metadata", {}), "metadata")),
    )


def evidence_from_wire(value: Any) -> EvidenceRef:
    data = _mapping(value, "evidence")
    kwargs: dict[str, Any] = {}
    if "observed_at" in data:
        kwargs["observed_at"] = _datetime(data["observed_at"], "observed_at")
    return EvidenceRef(
        evidence_id=data["evidence_id"],
        source_type=data["source_type"],
        source_uri=data["source_uri"],
        content_hash=data["content_hash"],
        metadata=dict(_mapping(data.get("metadata", {}), "metadata")),
        **kwargs,
    )


def audit_request_from_wire(value: Any) -> AuditRequest:
    data = _mapping(value, "audit request")
    kwargs: dict[str, Any] = {}
    if "requested_at" in data:
        kwargs["requested_at"] = _datetime(data["requested_at"], "requested_at")
    return AuditRequest(
        request_id=data["request_id"],
        agent_id=data["agent_id"],
        task_type=data["task_type"],
        inputs=dict(_mapping(data.get("inputs", {}), "inputs")),
        evidence=tuple(evidence_from_wire(item) for item in data.get("evidence", ())),
        correlation_id=data.get("correlation_id"),
        **kwargs,
    )


def rule_evaluation_from_wire(value: Any) -> RuleEvaluation:
    data = _mapping(value, "rule evaluation")
    return RuleEvaluation(
        rule_set_id=data["rule_set_id"],
        rule_set_version=data["rule_set_version"],
        matched_rules=_tuple_of_strings(data.get("matched_rules", ()), "matched_rules"),
        conclusions=_tuple_of_strings(data.get("conclusions", ()), "conclusions"),
        explanation_steps=_tuple_of_strings(
            data.get("explanation_steps", ()), "explanation_steps"
        ),
        missing_fields=_tuple_of_strings(data.get("missing_fields", ()), "missing_fields"),
        conflicts=_tuple_of_strings(data.get("conflicts", ()), "conflicts"),
    )


def audit_session_from_wire(value: Any) -> AuditSession:
    data = _mapping(value, "audit session")
    evaluation = data.get("rule_evaluation")
    return AuditSession(
        audit_id=data["audit_id"],
        request=audit_request_from_wire(data["request"]),
        profile=agent_profile_from_wire(data["profile"]),
        status=AuditStatus(data.get("status", AuditStatus.OPEN.value)),
        ontology_errors=_tuple_of_strings(data.get("ontology_errors", ()), "ontology_errors"),
        rule_evaluation=(
            rule_evaluation_from_wire(evaluation) if evaluation is not None else None
        ),
        decision_id=data.get("decision_id"),
    )


def decision_from_wire(value: Any) -> DecisionRecord:
    data = _mapping(value, "decision")
    kwargs: dict[str, Any] = {}
    if "created_at" in data:
        kwargs["created_at"] = _datetime(data["created_at"], "created_at")
    return DecisionRecord(
        decision_id=data["decision_id"],
        audit_id=data["audit_id"],
        agent_id=data["agent_id"],
        profile_version=data["profile_version"],
        category=data["category"],
        scenario=data["scenario"],
        outcome=data["outcome"],
        reasoning_summary=data["reasoning_summary"],
        confidence=float(data["confidence"]),
        evidence_ids=_tuple_of_strings(data.get("evidence_ids", ()), "evidence_ids"),
        rule_evaluation=rule_evaluation_from_wire(data["rule_evaluation"]),
        backend_name=data["backend_name"],
        backend_version=data["backend_version"],
        status=AuditStatus(data.get("status", AuditStatus.EVALUATED.value)),
        **kwargs,
    )


def approval_from_wire(value: Any) -> ApprovalRecord:
    data = _mapping(value, "approval")
    kwargs: dict[str, Any] = {}
    if "timestamp" in data:
        kwargs["timestamp"] = _datetime(data["timestamp"], "timestamp")
    return ApprovalRecord(
        approval_id=data["approval_id"],
        decision_id=data["decision_id"],
        approver_id=data["approver_id"],
        approver_role=data["approver_role"],
        action=data["action"],
        approval_method=data["approval_method"],
        approval_context=data["approval_context"],
        metadata=dict(_mapping(data.get("metadata", {}), "metadata")),
        **kwargs,
    )


def exception_from_wire(value: Any) -> PolicyExceptionRecord:
    data = _mapping(value, "policy exception")
    kwargs: dict[str, Any] = {}
    if "approved_at" in data:
        kwargs["approved_at"] = _datetime(data["approved_at"], "approved_at")
    return PolicyExceptionRecord(
        exception_id=data["exception_id"],
        decision_id=data["decision_id"],
        policy_id=data["policy_id"],
        reason=data["reason"],
        approver_id=data["approver_id"],
        approval_method=data["approval_method"],
        justification=data["justification"],
        metadata=dict(_mapping(data.get("metadata", {}), "metadata")),
        **kwargs,
    )


def trace_from_wire(value: Any) -> AuditTrace:
    data = _mapping(value, "audit trace")
    raw_nodes = data.get("nodes", ())
    if not isinstance(raw_nodes, (list, tuple)):
        raise ValueError("nodes must be an array")
    return AuditTrace(
        decision_id=data["decision_id"],
        agent_id=data["agent_id"],
        profile_version=data["profile_version"],
        rule_set_id=data["rule_set_id"],
        rule_set_version=data["rule_set_version"],
        decision_status=AuditStatus(
            data.get("decision_status", AuditStatus.EVALUATED.value)
        ),
        evidence=tuple(evidence_from_wire(item) for item in data.get("evidence", ())),
        nodes=tuple(dict(_mapping(item, "node")) for item in raw_nodes),
        approvals=tuple(approval_from_wire(item) for item in data.get("approvals", ())),
        exceptions=tuple(exception_from_wire(item) for item in data.get("exceptions", ())),
    )
