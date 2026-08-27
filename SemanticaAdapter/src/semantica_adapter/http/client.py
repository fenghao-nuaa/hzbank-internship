"""Synchronous Agent client for the stable SemanticaAdapter HTTP API."""

from __future__ import annotations

from hashlib import sha256
from hmac import compare_digest
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Mapping, TypeVar
from urllib.parse import quote

import httpx

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
    AuditTrace,
    DecisionRecord,
    PolicyExceptionRecord,
)

from .wire import (
    agent_profile_from_wire,
    audit_session_from_wire,
    decision_from_wire,
    exception_from_wire,
    to_wire,
    trace_from_wire,
)


_T = TypeVar("_T")


class SemanticaHttpClient:
    """Call a deployed governance service without importing Semantica."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("api_key must be a non-empty string")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    def __enter__(self) -> "SemanticaHttpClient":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client and not self._client.is_closed:
            self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                f"{self.base_url}{path}",
                headers={"X-API-Key": self.api_key},
                json=json,
            )
        except httpx.HTTPError as error:
            raise BackendError(f"SemanticaAdapter HTTP request failed: {error}") from error
        self._raise_remote_error(response)
        return response

    @staticmethod
    def _raise_remote_error(response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        try:
            payload = response.json()
            detail = payload.get("detail", response.text) if isinstance(payload, Mapping) else response.text
        except ValueError:
            detail = response.text or f"HTTP {response.status_code}"
        message = str(detail)
        if response.status_code == 403:
            raise ApprovalRequiredError(message)
        if response.status_code in {404, 422}:
            raise ValidationError(message)
        raise BackendError(message)

    def _json(self, method: str, path: str, *, json: Any | None = None) -> Any:
        response = self._request(method, path, json=json)
        try:
            return response.json()
        except ValueError as error:
            raise BackendError("SemanticaAdapter returned invalid JSON") from error

    def _model(
        self,
        method: str,
        path: str,
        decoder: Callable[[Any], _T],
        *,
        json: Any | None = None,
    ) -> _T:
        payload = self._json(method, path, json=json)
        try:
            return decoder(payload)
        except (KeyError, TypeError, ValueError) as error:
            raise BackendError(
                f"SemanticaAdapter returned an invalid response: {error}"
            ) from error

    def health_check(self) -> Mapping[str, Any]:
        payload = self._json("GET", "/health")
        if not isinstance(payload, Mapping):
            raise BackendError("SemanticaAdapter returned an invalid health response")
        return dict(payload)

    def register_agent(self, profile: AgentProfile) -> AgentProfile:
        return self._model(
            "POST", "/v1/agents", agent_profile_from_wire, json=to_wire(profile)
        )

    def start_audit(self, request: AuditRequest) -> AuditSession:
        return self._model(
            "POST", "/v1/audits", audit_session_from_wire, json=to_wire(request)
        )

    def evaluate(self, audit_id: str) -> AuditSession:
        encoded = quote(audit_id, safe="")
        return self._model(
            "POST", f"/v1/audits/{encoded}/evaluate", audit_session_from_wire
        )

    def record_decision(
        self,
        audit_id: str,
        proposed_outcome: str,
        reasoning_summary: str,
        confidence: float,
    ) -> DecisionRecord:
        encoded = quote(audit_id, safe="")
        return self._model(
            "POST",
            f"/v1/audits/{encoded}/decisions",
            decision_from_wire,
            json={
                "proposed_outcome": proposed_outcome,
                "reasoning_summary": reasoning_summary,
                "confidence": confidence,
            },
        )

    def submit_approval(self, approval: ApprovalRecord) -> DecisionRecord:
        return self._model(
            "POST", "/v1/approvals", decision_from_wire, json=to_wire(approval)
        )

    def record_exception(
        self, exception: PolicyExceptionRecord
    ) -> PolicyExceptionRecord:
        return self._model(
            "POST", "/v1/exceptions", exception_from_wire, json=to_wire(exception)
        )

    def get_audit_trace(self, decision_id: str) -> AuditTrace:
        encoded = quote(decision_id, safe="")
        return self._model(
            "GET", f"/v1/decisions/{encoded}/trace", trace_from_wire
        )

    def download_audit_package(self, decision_id: str, destination: Path) -> Path:
        encoded = quote(decision_id, safe="")
        response = self._request(
            "POST", f"/v1/decisions/{encoded}/audit-package"
        )
        expected = response.headers.get("X-Content-SHA256")
        actual = sha256(response.content).hexdigest()
        if expected is None:
            raise AuditIntegrityError("audit package response is missing its SHA-256 digest")
        if not compare_digest(expected.lower(), actual):
            raise AuditIntegrityError("audit package digest mismatch")

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with NamedTemporaryFile(
                mode="wb",
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(response.content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, destination)
        except Exception:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
            raise
        return destination
