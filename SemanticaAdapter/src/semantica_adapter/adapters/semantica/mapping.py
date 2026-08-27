"""Conversions between stable domain records and Semantica models."""

from collections.abc import Mapping, Sequence
from typing import Any

from semantica.context import ApprovalChain, Decision, PolicyException

from semantica_adapter.domain.models import (
    ApprovalRecord,
    AuditTrace,
    DecisionRecord,
    PolicyExceptionRecord,
)


def to_semantica_decision(record: DecisionRecord) -> Decision:
    return Decision(
        decision_id=record.decision_id,
        category=record.category,
        scenario=record.scenario,
        reasoning=record.reasoning_summary,
        outcome=record.outcome,
        confidence=record.confidence,
        timestamp=record.created_at,
        decision_maker=record.agent_id,
        metadata={
            "audit_id": record.audit_id,
            "profile_version": record.profile_version,
            "evidence_ids": list(record.evidence_ids),
            "backend_name": record.backend_name,
            "backend_version": record.backend_version,
            "status": record.status.value,
            "rule_set_id": record.rule_evaluation.rule_set_id,
            "rule_set_version": record.rule_evaluation.rule_set_version,
            "matched_rules": list(record.rule_evaluation.matched_rules),
        },
    )


def to_semantica_approval(record: ApprovalRecord) -> ApprovalChain:
    return ApprovalChain(
        approval_id=record.approval_id,
        decision_id=record.decision_id,
        approver=record.approver_id,
        approval_method=record.approval_method,
        approval_context=record.approval_context,
        timestamp=record.timestamp,
        metadata={
            **dict(record.metadata),
            "approver_role": record.approver_role,
            "action": record.action,
        },
    )


def to_semantica_exception(record: PolicyExceptionRecord) -> PolicyException:
    return PolicyException(
        exception_id=record.exception_id,
        decision_id=record.decision_id,
        policy_id=record.policy_id,
        reason=record.reason,
        approver=record.approver_id,
        approval_timestamp=record.approved_at,
        justification=record.justification,
        metadata={**dict(record.metadata), "approval_method": record.approval_method},
    )


def normalize_trace(
    raw: Sequence[Mapping[str, Any]],
    *,
    decision_id: str,
    agent_id: str,
    profile_version: str,
    rule_set_id: str,
    rule_set_version: str,
) -> AuditTrace:
    return AuditTrace(
        decision_id=decision_id,
        agent_id=agent_id,
        profile_version=profile_version,
        rule_set_id=rule_set_id,
        rule_set_version=rule_set_version,
        nodes=tuple(dict(item) for item in raw),
    )
