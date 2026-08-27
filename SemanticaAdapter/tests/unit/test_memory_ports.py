import pytest

from semantica_adapter.adapters.memory.approvals import MemoryApprovalWorkflow
from semantica_adapter.adapters.memory.backend import FakeGovernanceBackend
from semantica_adapter.adapters.memory.profiles import MemoryAgentProfileRepository
from semantica_adapter.domain.models import AgentProfile, ApprovalRecord
from semantica_adapter.ports.backend import GovernanceBackend


def _profile(version: str = "1.0") -> AgentProfile:
    return AgentProfile(
        agent_id="amount-checker",
        name="Amount Checker",
        purpose="Reconcile amounts",
        profile_version=version,
        rule_set_id="amount-rules",
        rule_set_version="2026.08",
        ontology_id="banking",
        ontology_version="1.0",
        allowed_source_types=("ledger", "voucher"),
        approval_policy="manual_on_review",
        required_fields=("declared_amount", "ledger_amount"),
    )


def test_fake_backend_satisfies_governance_contract() -> None:
    assert isinstance(FakeGovernanceBackend(), GovernanceBackend)


def test_profile_repository_preserves_versions_and_latest() -> None:
    repository = MemoryAgentProfileRepository()
    repository.save(_profile("1.0"))
    repository.save(_profile("2.0"))
    assert repository.get("amount-checker", "1.0").profile_version == "1.0"
    assert repository.get("amount-checker").profile_version == "2.0"
    with pytest.raises(ValueError, match="already exists"):
        repository.save(_profile("2.0"))


def test_profile_repository_rejects_unknown_agent() -> None:
    with pytest.raises(KeyError, match="unknown Agent profile"):
        MemoryAgentProfileRepository().get("missing")


def test_approval_workflow_rejects_agent_self_approval() -> None:
    workflow = MemoryApprovalWorkflow({("risk-manager", "reviewer")})
    approval = ApprovalRecord(
        approval_id="approval-1",
        decision_id="decision-1",
        approver_id="agent-process",
        approver_role="agent",
        action="approve",
        approval_method="system",
        approval_context="self approval",
    )
    assert workflow.authorize(approval) is False
