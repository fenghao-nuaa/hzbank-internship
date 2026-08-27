from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from semantica_adapter.adapters.memory import (
    FakeGovernanceBackend,
    MemoryAgentProfileRepository,
    MemoryApprovalWorkflow,
)
from semantica_adapter.domain.errors import ConfigurationError
from semantica_adapter.domain.models import (
    AgentProfile,
    ApprovalRecord,
    AuditExport,
    AuditRequest,
    EvidenceRef,
    PolicyExceptionRecord,
)
from semantica_adapter.http.app import create_app
from semantica_adapter.http.runtime import load_runtime_config
from semantica_adapter.http.wire import to_wire
from semantica_adapter.services.governance import AgentGovernanceService


API_KEY = "test-api-key-123456"
AUTH = {"X-API-Key": API_KEY}


class PackageBackend(FakeGovernanceBackend):
    def export_decision(self, decision_id: str, output_dir: Path, format: str) -> AuditExport:
        if format == "json":
            return super().export_decision(decision_id, output_dir, format)
        if format != "turtle":
            return super().export_decision(decision_id, output_dir, format)
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / "decision.ttl"
        target.write_text(
            f'@prefix audit: <urn:audit:> .\naudit:decision audit:id "{decision_id}" .\n',
            encoding="utf-8",
        )
        return AuditExport(
            decision_id,
            format,
            target,
            sha256(target.read_bytes()).hexdigest(),
        )


def _service() -> AgentGovernanceService:
    return AgentGovernanceService(
        PackageBackend(),
        MemoryAgentProfileRepository(),
        MemoryApprovalWorkflow({("risk-manager", "reviewer")}),
    )


def _profile() -> AgentProfile:
    return AgentProfile(
        agent_id="amount-checker",
        name="Amount Checker",
        purpose="Reconcile amounts",
        profile_version="1.0",
        rule_set_id="amount-rules",
        rule_set_version="2026.08",
        ontology_id="banking",
        ontology_version="1.0",
        allowed_source_types=("ledger",),
        approval_policy="manual_on_review",
        required_fields=("declared_amount", "ledger_amount"),
        ontology={"types": {"declared_amount": "int", "ledger_amount": "int"}},
    )


def _request() -> AuditRequest:
    return AuditRequest(
        request_id="request-http-1",
        agent_id="amount-checker",
        task_type="amount_reconciliation",
        inputs={"declared_amount": 101, "ledger_amount": 100},
        evidence=(
            EvidenceRef(
                "ledger-1",
                "ledger",
                "ledger://entry/1",
                sha256(b"100").hexdigest(),
            ),
        ),
    )


def test_v1_routes_require_valid_api_key() -> None:
    client = TestClient(create_app(_service(), api_key=API_KEY))

    assert client.get("/health").status_code == 200
    assert client.post("/v1/agents", json=to_wire(_profile())).status_code == 401
    assert (
        client.post(
            "/v1/agents",
            headers={"X-API-Key": "wrong"},
            json=to_wire(_profile()),
        ).status_code
        == 401
    )


def test_malformed_json_object_is_validation_error_not_missing_resource() -> None:
    client = TestClient(create_app(_service(), api_key=API_KEY))
    payload = to_wire(_profile())
    del payload["agent_id"]

    response = client.post("/v1/agents", headers=AUTH, json=payload)

    assert response.status_code == 422
    assert "agent_id" in response.json()["detail"]


def test_invalid_decision_field_type_returns_validation_error() -> None:
    client = TestClient(create_app(_service(), api_key=API_KEY))
    client.post("/v1/agents", headers=AUTH, json=to_wire(_profile()))
    audit = client.post("/v1/audits", headers=AUTH, json=to_wire(_request())).json()
    client.post(f"/v1/audits/{audit['audit_id']}/evaluate", headers=AUTH)

    response = client.post(
        f"/v1/audits/{audit['audit_id']}/decisions",
        headers=AUTH,
        json={
            "proposed_outcome": "manual_review",
            "reasoning_summary": "Amounts differ",
            "confidence": None,
        },
    )

    assert response.status_code == 422


def test_http_api_runs_full_governance_lifecycle() -> None:
    client = TestClient(create_app(_service(), api_key=API_KEY))

    profile_response = client.post("/v1/agents", headers=AUTH, json=to_wire(_profile()))
    assert profile_response.status_code == 201

    audit_response = client.post("/v1/audits", headers=AUTH, json=to_wire(_request()))
    assert audit_response.status_code == 201
    audit_id = audit_response.json()["audit_id"]

    evaluation = client.post(f"/v1/audits/{audit_id}/evaluate", headers=AUTH)
    assert evaluation.status_code == 200
    assert evaluation.json()["status"] == "manual_review"

    decision_response = client.post(
        f"/v1/audits/{audit_id}/decisions",
        headers=AUTH,
        json={
            "proposed_outcome": "matched",
            "reasoning_summary": "Agent proposal is constrained by deterministic rules",
            "confidence": 1.0,
        },
    )
    assert decision_response.status_code == 201
    decision_id = decision_response.json()["decision_id"]
    assert decision_response.json()["outcome"] == "manual_review"
    assert decision_response.json()["status"] == "pending_approval"

    exception = PolicyExceptionRecord(
        "exception-http-1",
        decision_id,
        "POL-1",
        "approved discrepancy",
        "risk-manager",
        "email",
        "secondary control",
        metadata={"approver_role": "reviewer"},
    )
    exception_response = client.post(
        "/v1/exceptions", headers=AUTH, json=to_wire(exception)
    )
    assert exception_response.status_code == 201

    approval = ApprovalRecord(
        "approval-http-1",
        decision_id,
        "risk-manager",
        "reviewer",
        "approve",
        "email",
        "ledger evidence reviewed",
    )
    approval_response = client.post(
        "/v1/approvals", headers=AUTH, json=to_wire(approval)
    )
    assert approval_response.status_code == 200
    assert approval_response.json()["status"] == "approved"

    trace_response = client.get(f"/v1/decisions/{decision_id}/trace", headers=AUTH)
    assert trace_response.status_code == 200
    trace = trace_response.json()
    assert trace["decision_status"] == "approved"
    assert [item["approval_id"] for item in trace["approvals"]] == ["approval-http-1"]
    assert [item["exception_id"] for item in trace["exceptions"]] == ["exception-http-1"]


def test_http_api_returns_verifiable_audit_package_zip() -> None:
    client = TestClient(create_app(_service(), api_key=API_KEY))
    client.post("/v1/agents", headers=AUTH, json=to_wire(_profile()))
    audit = client.post("/v1/audits", headers=AUTH, json=to_wire(_request())).json()
    client.post(f"/v1/audits/{audit['audit_id']}/evaluate", headers=AUTH)
    decision = client.post(
        f"/v1/audits/{audit['audit_id']}/decisions",
        headers=AUTH,
        json={
            "proposed_outcome": "manual_review",
            "reasoning_summary": "Amounts differ",
            "confidence": 1.0,
        },
    ).json()

    response = client.post(
        f"/v1/decisions/{decision['decision_id']}/audit-package", headers=AUTH
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["x-content-sha256"] == sha256(response.content).hexdigest()
    with ZipFile(BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {"manifest.json", "audit-chain.json", "decision.ttl"} <= names
        assert any(name.endswith(".json") and name != "manifest.json" for name in names)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["decision_id"] == decision["decision_id"]


def test_runtime_configuration_requires_secret_and_valid_actor_pairs() -> None:
    with pytest.raises(ConfigurationError, match="SEMANTICA_ADAPTER_API_KEY"):
        load_runtime_config({})
    with pytest.raises(ConfigurationError, match="AUTHORIZED_ACTORS"):
        load_runtime_config(
            {
                "SEMANTICA_ADAPTER_API_KEY": API_KEY,
                "SEMANTICA_ADAPTER_AUTHORIZED_ACTORS": "not-json",
            }
        )

    config = load_runtime_config(
        {
            "SEMANTICA_ADAPTER_API_KEY": API_KEY,
            "SEMANTICA_ADAPTER_AUTHORIZED_ACTORS": json.dumps(
                [["risk-manager", "reviewer"]]
            ),
        }
    )
    assert config.authorized_actors == frozenset({("risk-manager", "reviewer")})
    assert config.host == "127.0.0.1"
    assert config.port == 8001
