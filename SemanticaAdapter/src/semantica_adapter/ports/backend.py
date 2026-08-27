"""Contract implemented by Semantica and future governance providers."""

from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from semantica_adapter.domain.models import (
    AgentProfile,
    ApprovalRecord,
    AuditExport,
    AuditTrace,
    AuditStatus,
    DecisionRecord,
    EvidenceRef,
    PolicyExceptionRecord,
    RuleEvaluation,
)


@runtime_checkable
class GovernanceBackend(Protocol):
    name: str
    version: str

    def capabilities(self) -> frozenset[str]: ...

    def health_check(self) -> Mapping[str, Any]: ...

    def record_profile_snapshot(self, audit_id: str, profile: AgentProfile) -> str: ...

    def record_evidence(self, audit_id: str, evidence: EvidenceRef) -> str: ...

    def validate_ontology(
        self, audit_id: str, profile: AgentProfile, inputs: Mapping[str, Any]
    ) -> tuple[str, ...]: ...

    def evaluate_rules(
        self, audit_id: str, profile: AgentProfile, inputs: Mapping[str, Any]
    ) -> RuleEvaluation: ...

    def record_decision(self, decision: DecisionRecord) -> str: ...

    def update_decision_status(
        self, decision_id: str, status: AuditStatus
    ) -> None: ...

    def record_approval(self, approval: ApprovalRecord) -> str: ...

    def record_exception(self, exception: PolicyExceptionRecord) -> str: ...

    def trace_decision(self, decision_id: str) -> AuditTrace: ...

    def export_decision(self, decision_id: str, output_dir: Path, format: str) -> AuditExport: ...
