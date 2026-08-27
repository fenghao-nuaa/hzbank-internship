"""Provider-neutral in-memory backend for interface contract tests."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from semantica_adapter.domain.errors import UnsupportedCapabilityError
from semantica_adapter.domain.models import (
    AgentProfile,
    ApprovalRecord,
    AuditExport,
    AuditStatus,
    AuditTrace,
    DecisionRecord,
    EvidenceRef,
    PolicyExceptionRecord,
    RuleEvaluation,
)


class FakeGovernanceBackend:
    name = "fake"
    version = "1.0"

    def __init__(self) -> None:
        self.profiles: dict[str, AgentProfile] = {}
        self.evidence: dict[str, dict[str, EvidenceRef]] = {}
        self.decisions: dict[str, DecisionRecord] = {}
        self.approvals: dict[str, list[ApprovalRecord]] = {}
        self.approvals_by_id: dict[str, ApprovalRecord] = {}
        self.exceptions: dict[str, list[PolicyExceptionRecord]] = {}
        self.exceptions_by_id: dict[str, PolicyExceptionRecord] = {}

    def capabilities(self) -> frozenset[str]:
        return frozenset(
            {"context", "reasoning", "provenance", "ontology", "approval", "exception", "json_export"}
        )

    def health_check(self) -> Mapping[str, Any]:
        return {"healthy": True, "backend": self.name, "version": self.version}

    def record_profile_snapshot(self, audit_id: str, profile: AgentProfile) -> str:
        self.profiles[audit_id] = profile
        return f"profile:{profile.agent_id}:{profile.profile_version}:{audit_id}"

    def record_evidence(self, audit_id: str, evidence: EvidenceRef) -> str:
        self.evidence.setdefault(audit_id, {})[evidence.evidence_id] = evidence
        return evidence.evidence_id

    def validate_ontology(
        self, audit_id: str, profile: AgentProfile, inputs: Mapping[str, Any]
    ) -> tuple[str, ...]:
        errors: list[str] = []
        expected_types = profile.ontology.get("types", {})
        for field_name, type_name in expected_types.items():
            value = inputs.get(field_name)
            if value is not None and type(value).__name__ != type_name:
                errors.append(f"{field_name} must be {type_name}")
        return tuple(errors)

    def evaluate_rules(
        self, audit_id: str, profile: AgentProfile, inputs: Mapping[str, Any]
    ) -> RuleEvaluation:
        missing = tuple(field for field in profile.required_fields if field not in inputs)
        matched: tuple[str, ...] = ()
        conclusions: tuple[str, ...] = ()
        steps: tuple[str, ...] = ()
        if not missing and {"declared_amount", "ledger_amount"}.issubset(inputs):
            equal = inputs["declared_amount"] == inputs["ledger_amount"]
            matched = ("amount-equality",)
            conclusions = ("matched" if equal else "mismatch",)
            steps = (f"declared_amount {'==' if equal else '!='} ledger_amount",)
        conflicts = tuple(str(item) for item in inputs.get("_conflicts", ()))
        return RuleEvaluation(
            profile.rule_set_id,
            profile.rule_set_version,
            matched_rules=matched,
            conclusions=conclusions,
            explanation_steps=steps,
            missing_fields=missing,
            conflicts=conflicts,
        )

    def record_decision(self, decision: DecisionRecord) -> str:
        self.decisions[decision.decision_id] = decision
        return decision.decision_id

    def update_decision_status(self, decision_id: str, status: AuditStatus) -> None:
        self.decisions[decision_id] = replace(self.decisions[decision_id], status=status)

    def record_approval(self, approval: ApprovalRecord) -> str:
        existing = self.approvals_by_id.get(approval.approval_id)
        if existing is not None:
            if existing != approval:
                raise ValueError(f"approval ID collision: {approval.approval_id}")
            return approval.approval_id
        self.approvals.setdefault(approval.decision_id, []).append(approval)
        self.approvals_by_id[approval.approval_id] = approval
        return approval.approval_id

    def record_exception(self, exception: PolicyExceptionRecord) -> str:
        existing = self.exceptions_by_id.get(exception.exception_id)
        if existing is not None:
            if existing != exception:
                raise ValueError(f"exception ID collision: {exception.exception_id}")
            return exception.exception_id
        self.exceptions.setdefault(exception.decision_id, []).append(exception)
        self.exceptions_by_id[exception.exception_id] = exception
        return exception.exception_id

    def trace_decision(self, decision_id: str) -> AuditTrace:
        decision = self.decisions[decision_id]
        profile = self.profiles[decision.audit_id]
        evidence = tuple(
            item
            for evidence_id in decision.evidence_ids
            if (item := self.evidence.get(decision.audit_id, {}).get(evidence_id)) is not None
        )
        return AuditTrace(
            decision_id=decision_id,
            agent_id=decision.agent_id,
            profile_version=decision.profile_version,
            rule_set_id=decision.rule_evaluation.rule_set_id,
            rule_set_version=decision.rule_evaluation.rule_set_version,
            decision_status=decision.status,
            evidence=evidence,
            nodes=(asdict(decision),),
            approvals=tuple(self.approvals.get(decision_id, [])),
            exceptions=tuple(self.exceptions.get(decision_id, [])),
        )

    def export_decision(self, decision_id: str, output_dir: Path, format: str) -> AuditExport:
        if format != "json":
            raise UnsupportedCapabilityError(f"fake backend does not support {format}")
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / f"{decision_id}.json"
        target.write_text(
            json.dumps(asdict(self.trace_decision(decision_id)), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return AuditExport(decision_id, format, target, sha256(target.read_bytes()).hexdigest())
