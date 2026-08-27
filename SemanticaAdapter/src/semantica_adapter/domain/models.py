"""Provider-neutral records used at the company-Agent boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from string import hexdigits
from typing import Any, Mapping


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in hexdigits for character in value):
        raise ValueError("content hash must be a SHA-256 hexadecimal digest")


class AuditStatus(str, Enum):
    OPEN = "open"
    EVALUATED = "evaluated"
    MANUAL_REVIEW = "manual_review"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class AgentProfile:
    agent_id: str
    name: str
    purpose: str
    profile_version: str
    rule_set_id: str
    rule_set_version: str
    ontology_id: str
    ontology_version: str
    allowed_source_types: tuple[str, ...]
    approval_policy: str
    required_fields: tuple[str, ...] = ()
    rules: Mapping[str, Any] = field(default_factory=dict)
    ontology: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "agent_id",
            "name",
            "purpose",
            "profile_version",
            "rule_set_id",
            "rule_set_version",
            "ontology_id",
            "ontology_version",
        ):
            _require_text(getattr(self, name), name)
        if self.approval_policy not in {"always", "manual_on_review", "never"}:
            raise ValueError("approval_policy must be always, manual_on_review, or never")


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    source_type: str
    source_uri: str
    content_hash: str
    observed_at: datetime = field(default_factory=_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.evidence_id, "evidence_id")
        _require_text(self.source_type, "source_type")
        _require_text(self.source_uri, "source_uri")
        _require_sha256(self.content_hash)


@dataclass(frozen=True, slots=True)
class AuditRequest:
    request_id: str
    agent_id: str
    task_type: str
    inputs: Mapping[str, Any]
    evidence: tuple[EvidenceRef, ...]
    requested_at: datetime = field(default_factory=_now)
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.request_id, "request_id")
        _require_text(self.agent_id, "agent_id")
        _require_text(self.task_type, "task_type")


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    rule_set_id: str
    rule_set_version: str
    matched_rules: tuple[str, ...] = ()
    conclusions: tuple[str, ...] = ()
    explanation_steps: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AuditSession:
    audit_id: str
    request: AuditRequest
    profile: AgentProfile
    status: AuditStatus = AuditStatus.OPEN
    ontology_errors: tuple[str, ...] = ()
    rule_evaluation: RuleEvaluation | None = None
    decision_id: str | None = None


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    decision_id: str
    audit_id: str
    agent_id: str
    profile_version: str
    category: str
    scenario: str
    outcome: str
    reasoning_summary: str
    confidence: float
    evidence_ids: tuple[str, ...]
    rule_evaluation: RuleEvaluation
    backend_name: str
    backend_version: str
    created_at: datetime = field(default_factory=_now)
    status: AuditStatus = AuditStatus.EVALUATED

    def __post_init__(self) -> None:
        for name in (
            "decision_id",
            "audit_id",
            "agent_id",
            "profile_version",
            "category",
            "scenario",
            "outcome",
            "reasoning_summary",
            "backend_name",
            "backend_version",
        ):
            _require_text(getattr(self, name), name)
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


_APPROVAL_METHODS = {"slack_dm", "zoom_call", "email", "system"}


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    decision_id: str
    approver_id: str
    approver_role: str
    action: str
    approval_method: str
    approval_context: str
    timestamp: datetime = field(default_factory=_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.approval_method not in _APPROVAL_METHODS:
            raise ValueError(f"approval_method must be one of {sorted(_APPROVAL_METHODS)}")
        if self.action not in {"approve", "reject"}:
            raise ValueError("action must be approve or reject")


@dataclass(frozen=True, slots=True)
class PolicyExceptionRecord:
    exception_id: str
    decision_id: str
    policy_id: str
    reason: str
    approver_id: str
    approval_method: str
    justification: str
    approved_at: datetime = field(default_factory=_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.approval_method not in _APPROVAL_METHODS:
            raise ValueError(f"approval_method must be one of {sorted(_APPROVAL_METHODS)}")


@dataclass(frozen=True, slots=True)
class AuditTrace:
    decision_id: str
    agent_id: str
    profile_version: str
    rule_set_id: str
    rule_set_version: str
    decision_status: AuditStatus = AuditStatus.EVALUATED
    evidence: tuple[EvidenceRef, ...] = ()
    nodes: tuple[Mapping[str, Any], ...] = ()
    approvals: tuple[ApprovalRecord, ...] = ()
    exceptions: tuple[PolicyExceptionRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class AuditExport:
    decision_id: str
    format: str
    path: Path
    sha256: str
    generated_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        _require_sha256(self.sha256)
