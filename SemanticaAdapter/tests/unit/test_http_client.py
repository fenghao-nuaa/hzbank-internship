from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

from semantica_adapter.domain.errors import (
    ApprovalRequiredError,
    AuditIntegrityError,
    BackendError,
    ValidationError,
)
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
from semantica_adapter.http.client import SemanticaHttpClient
from semantica_adapter.http.wire import to_wire


NOW = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
API_KEY = "agent-secret-key"


def _profile() -> AgentProfile:
    return AgentProfile(
        "amount-checker",
        "Amount Checker",
        "Reconcile amounts",
        "1.0",
        "amount-rules",
        "2026.08",
        "banking",
        "1.0",
        ("ledger",),
        "manual_on_review",
        ("declared_amount", "ledger_amount"),
    )


def _request() -> AuditRequest:
    evidence = EvidenceRef(
        "ledger-1",
        "ledger",
        "ledger://entry/1",
        sha256(b"100").hexdigest(),
        NOW,
    )
    return AuditRequest(
        "request-1",
        "amount-checker",
        "amount_reconciliation",
        {"declared_amount": 101, "ledger_amount": 100},
        (evidence,),
        NOW,
    )


def _evaluation() -> RuleEvaluation:
    return RuleEvaluation(
        "amount-rules",
        "2026.08",
        ("amount-equality",),
        ("mismatch",),
        ("declared_amount != ledger_amount",),
    )


def _session() -> AuditSession:
    return AuditSession(
        "audit:request-1",
        _request(),
        _profile(),
        AuditStatus.MANUAL_REVIEW,
        rule_evaluation=_evaluation(),
    )


def _decision() -> DecisionRecord:
    return DecisionRecord(
        "decision:audit:request-1",
        "audit:request-1",
        "amount-checker",
        "1.0",
        "amount_reconciliation",
        "Compare amounts",
        "manual_review",
        "Amounts differ",
        1.0,
        ("ledger-1",),
        _evaluation(),
        "semantica",
        "0.6.6",
        NOW,
        AuditStatus.PENDING_APPROVAL,
    )


def _approval() -> ApprovalRecord:
    return ApprovalRecord(
        "approval-1",
        _decision().decision_id,
        "risk-manager",
        "reviewer",
        "approve",
        "email",
        "reviewed",
        NOW,
    )


def _exception() -> PolicyExceptionRecord:
    return PolicyExceptionRecord(
        "exception-1",
        _decision().decision_id,
        "POL-1",
        "approved discrepancy",
        "risk-manager",
        "email",
        "secondary control",
        NOW,
        {"approver_role": "reviewer"},
    )


def _trace() -> AuditTrace:
    return AuditTrace(
        _decision().decision_id,
        "amount-checker",
        "1.0",
        "amount-rules",
        "2026.08",
        AuditStatus.APPROVED,
        _request().evidence,
        ({"id": _decision().decision_id, "type": "Decision"},),
        (_approval(),),
        (_exception(),),
    )


def test_client_calls_full_lifecycle_with_authentication_and_decodes_models() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == API_KEY
        seen.append((request.method, request.url.path))
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"healthy": True, "backend": "semantica"})
        if path == "/v1/agents":
            return httpx.Response(201, json=to_wire(_profile()))
        if path == "/v1/audits":
            return httpx.Response(201, json=to_wire(_session()))
        if path.endswith("/evaluate"):
            return httpx.Response(200, json=to_wire(_session()))
        if path.endswith("/decisions"):
            return httpx.Response(201, json=to_wire(_decision()))
        if path == "/v1/approvals":
            return httpx.Response(200, json=to_wire(_decision()))
        if path == "/v1/exceptions":
            return httpx.Response(201, json=to_wire(_exception()))
        if path.endswith("/trace"):
            return httpx.Response(200, json=to_wire(_trace()))
        raise AssertionError(f"unexpected path: {path}")

    transport = httpx.MockTransport(handler)
    injected = httpx.Client(transport=transport)
    client = SemanticaHttpClient(
        "https://governance.bank.example/", API_KEY, client=injected
    )

    assert client.health_check()["healthy"] is True
    assert client.register_agent(_profile()) == _profile()
    assert client.start_audit(_request()) == _session()
    assert client.evaluate(_session().audit_id) == _session()
    assert client.record_decision(
        _session().audit_id, "matched", "Agent proposal", 1.0
    ) == _decision()
    assert client.record_exception(_exception()) == _exception()
    assert client.submit_approval(_approval()) == _decision()
    assert client.get_audit_trace(_decision().decision_id) == _trace()
    client.close()

    assert not injected.is_closed
    assert ("POST", "/v1/audits/audit:request-1/evaluate") in seen


@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (401, BackendError),
        (403, ApprovalRequiredError),
        (404, ValidationError),
        (422, ValidationError),
        (502, BackendError),
    ],
)
def test_client_maps_http_errors(status_code: int, expected_error: type[Exception]) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status_code, json={"detail": "remote failure"})
    )
    with httpx.Client(transport=transport) as injected:
        client = SemanticaHttpClient("https://example.test", API_KEY, client=injected)
        with pytest.raises(expected_error, match="remote failure"):
            client.health_check()


def test_client_translates_transport_and_invalid_json_errors() -> None:
    def disconnected(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(disconnected)) as injected:
        client = SemanticaHttpClient("https://example.test", API_KEY, client=injected)
        with pytest.raises(BackendError, match="request failed"):
            client.health_check()

    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="not-json"))
    with httpx.Client(transport=transport) as injected:
        client = SemanticaHttpClient("https://example.test", API_KEY, client=injected)
        with pytest.raises(BackendError, match="invalid JSON"):
            client.health_check()


def test_client_downloads_package_only_after_sha256_verification(tmp_path: Path) -> None:
    payload = b"PK\x03\x04audit-package"

    def valid_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={"X-Content-SHA256": sha256(payload).hexdigest()},
        )

    destination = tmp_path / "downloads" / "audit.zip"
    with httpx.Client(transport=httpx.MockTransport(valid_handler)) as injected:
        client = SemanticaHttpClient("https://example.test", API_KEY, client=injected)
        assert client.download_audit_package(_decision().decision_id, destination) == destination
    assert destination.read_bytes() == payload

    def invalid_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"X-Content-SHA256": "0" * 64})

    rejected = tmp_path / "rejected.zip"
    with httpx.Client(transport=httpx.MockTransport(invalid_handler)) as injected:
        client = SemanticaHttpClient("https://example.test", API_KEY, client=injected)
        with pytest.raises(AuditIntegrityError, match="digest mismatch"):
            client.download_audit_package(_decision().decision_id, rejected)
    assert not rejected.exists()
