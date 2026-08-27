"""Graph-backed decisions, approvals, policy exceptions, and provenance links."""

from __future__ import annotations

from collections import defaultdict

from auditgraph.core.models import Approval, Decision, PolicyException
from auditgraph.provenance import ProvenanceManager

from .context_graph import ContextGraph


class DecisionRecorder:
    def __init__(self, graph: ContextGraph, provenance: ProvenanceManager) -> None:
        self.graph = graph
        self.provenance = provenance
        self._approvals: dict[str, list[Approval]] = defaultdict(list)
        self._exceptions: dict[str, list[PolicyException]] = defaultdict(list)

    def record_decision(
        self,
        decision: Decision,
        *,
        evidence_ids: list[str] | None = None,
        rule_refs: list[tuple[str, str]] | None = None,
    ) -> Decision:
        evidence_ids = list(evidence_ids or [])
        rule_node_ids = [f"{policy_id}:{version}" for policy_id, version in (rule_refs or [])]
        for node_id in [*evidence_ids, *rule_node_ids]:
            if self.graph.get_node(node_id) is None:
                raise KeyError(f"unknown evidence or policy node: {node_id}")

        self.graph.add_node(
            decision.decision_id,
            "decision",
            **{key: value for key, value in decision.to_dict().items() if key != "decision_id"},
        )
        for evidence_id in evidence_ids:
            self.graph.add_edge(decision.decision_id, evidence_id, "USED_EVIDENCE")
            if not self._has_provenance(evidence_id):
                evidence = self.graph.get_node(evidence_id) or {}
                self.provenance.track(evidence_id, "evidence", evidence.get("properties", {}))
        for policy_node_id in rule_node_ids:
            self.graph.add_edge(decision.decision_id, policy_node_id, "APPLIED_POLICY")
            if not self._has_provenance(policy_node_id):
                policy = self.graph.get_node(policy_node_id) or {}
                self.provenance.track(policy_node_id, "policy", policy.get("properties", {}))

        self.provenance.track(
            decision.decision_id,
            "decision",
            decision.to_dict(),
            derived_from=[*rule_node_ids, *evidence_ids],
            agent_id=decision.decision_maker,
        )
        return decision

    def record_approval(self, approval: Approval) -> Approval:
        if self.graph.get_node(approval.decision_id) is None:
            raise KeyError(f"unknown decision: {approval.decision_id}")
        self.graph.add_node(approval.approval_id, "approval", **approval.to_dict())
        self.graph.add_edge(approval.decision_id, approval.approval_id, "HAS_APPROVAL")
        self._approvals[approval.decision_id].append(approval)
        self.provenance.track(
            approval.approval_id,
            "approval",
            approval.to_dict(),
            derived_from=[approval.decision_id],
            agent_id=approval.approver,
        )
        return approval

    def record_policy_exception(self, exception: PolicyException) -> PolicyException:
        if self.graph.get_node(exception.decision_id) is None:
            raise KeyError(f"unknown decision: {exception.decision_id}")
        self.graph.add_node(exception.exception_id, "policy_exception", **exception.to_dict())
        self.graph.add_edge(exception.decision_id, exception.exception_id, "HAS_POLICY_EXCEPTION")
        self._exceptions[exception.decision_id].append(exception)
        self.provenance.track(
            exception.exception_id,
            "policy_exception",
            exception.to_dict(),
            derived_from=[exception.decision_id],
            agent_id=exception.approver,
        )
        return exception

    def get_approvals(self, decision_id: str) -> list[Approval]:
        return list(self._approvals.get(decision_id, []))

    def get_policy_exceptions(self, decision_id: str) -> list[PolicyException]:
        return list(self._exceptions.get(decision_id, []))

    def _has_provenance(self, entity_id: str) -> bool:
        return any(entry.entity_id == entity_id for entry in self.provenance.storage.all())
