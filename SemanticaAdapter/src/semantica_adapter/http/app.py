"""FastAPI facade over the provider-neutral governance service."""

from __future__ import annotations

from hashlib import sha256
from hmac import compare_digest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, TypeVar
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse, Response

from semantica_adapter.domain.errors import (
    ApprovalRequiredError,
    BackendError,
    ConfigurationError,
    SemanticaAdapterError,
    ValidationError,
)
from semantica_adapter.services.governance import AgentGovernanceService

from .wire import (
    agent_profile_from_wire,
    approval_from_wire,
    audit_request_from_wire,
    exception_from_wire,
    to_wire,
)


_T = TypeVar("_T")


def _error(status_code: int, error: Exception) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": str(error)})


def _decode(decoder: Callable[[Any], _T], payload: Any) -> _T:
    try:
        return decoder(payload)
    except KeyError as error:
        field = error.args[0] if error.args else "unknown"
        raise ValueError(f"missing required field: {field}") from error


def _required(payload: dict[str, Any], field: str) -> Any:
    try:
        return payload[field]
    except KeyError as error:
        raise ValueError(f"missing required field: {field}") from error


def create_app(service: AgentGovernanceService, *, api_key: str) -> FastAPI:
    """Create an authenticated HTTP facade for one governance service instance."""

    if not isinstance(api_key, str) or not api_key:
        raise ConfigurationError("api_key must be configured")

    app = FastAPI(
        title="SemanticaAdapter Governance API",
        version="0.1.0",
        description="Stable HTTP governance boundary for company Agents.",
    )

    def require_api_key(x_api_key: str | None = Header(None, alias="X-API-Key")) -> None:
        if x_api_key is None or not compare_digest(x_api_key, api_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing API key",
            )

    protected = [Depends(require_api_key)]

    @app.exception_handler(ApprovalRequiredError)
    async def approval_error(_request, error: ApprovalRequiredError) -> JSONResponse:
        return _error(status.HTTP_403_FORBIDDEN, error)

    @app.exception_handler(ValidationError)
    async def validation_error(_request, error: ValidationError) -> JSONResponse:
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, error)

    @app.exception_handler(ValueError)
    async def value_error(_request, error: ValueError) -> JSONResponse:
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, error)

    @app.exception_handler(TypeError)
    async def type_error(_request, error: TypeError) -> JSONResponse:
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, error)

    @app.exception_handler(KeyError)
    async def missing_error(_request, error: KeyError) -> JSONResponse:
        detail = error.args[0] if error.args else "resource not found"
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(detail)}
        )

    @app.exception_handler(BackendError)
    async def backend_error(_request, error: BackendError) -> JSONResponse:
        return _error(status.HTTP_502_BAD_GATEWAY, error)

    @app.exception_handler(SemanticaAdapterError)
    async def adapter_error(_request, error: SemanticaAdapterError) -> JSONResponse:
        return _error(status.HTTP_400_BAD_REQUEST, error)

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse(content=to_wire(service.backend.health_check()))

    @app.post("/v1/agents", status_code=status.HTTP_201_CREATED, dependencies=protected)
    def register_agent(payload: dict[str, Any]) -> JSONResponse:
        profile = service.register_agent(_decode(agent_profile_from_wire, payload))
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=to_wire(profile))

    @app.post("/v1/audits", status_code=status.HTTP_201_CREATED, dependencies=protected)
    def start_audit(payload: dict[str, Any]) -> JSONResponse:
        audit = service.start_audit(_decode(audit_request_from_wire, payload))
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=to_wire(audit))

    @app.post("/v1/audits/{audit_id}/evaluate", dependencies=protected)
    def evaluate(audit_id: str) -> JSONResponse:
        return JSONResponse(content=to_wire(service.evaluate(audit_id)))

    @app.post(
        "/v1/audits/{audit_id}/decisions",
        status_code=status.HTTP_201_CREATED,
        dependencies=protected,
    )
    def record_decision(audit_id: str, payload: dict[str, Any]) -> JSONResponse:
        decision = service.record_decision(
            audit_id,
            proposed_outcome=_required(payload, "proposed_outcome"),
            reasoning_summary=_required(payload, "reasoning_summary"),
            confidence=float(_required(payload, "confidence")),
        )
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=to_wire(decision))

    @app.post("/v1/approvals", dependencies=protected)
    def submit_approval(payload: dict[str, Any]) -> JSONResponse:
        decision = service.submit_approval(_decode(approval_from_wire, payload))
        return JSONResponse(content=to_wire(decision))

    @app.post(
        "/v1/exceptions", status_code=status.HTTP_201_CREATED, dependencies=protected
    )
    def record_exception(payload: dict[str, Any]) -> JSONResponse:
        exception = service.record_exception(_decode(exception_from_wire, payload))
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=to_wire(exception))

    @app.get("/v1/decisions/{decision_id}/trace", dependencies=protected)
    def get_trace(decision_id: str) -> JSONResponse:
        return JSONResponse(content=to_wire(service.get_audit_trace(decision_id)))

    @app.post("/v1/decisions/{decision_id}/audit-package", dependencies=protected)
    def audit_package(decision_id: str) -> Response:
        with TemporaryDirectory(prefix="semantica-adapter-http-") as temporary:
            output_dir = Path(temporary) / "audit-package"
            package = service.export_audit_package(decision_id, output_dir)
            buffer = BytesIO()
            with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as archive:
                for artifact in sorted(package.output_dir.iterdir(), key=lambda item: item.name):
                    if artifact.is_file() and not artifact.is_symlink():
                        archive.write(artifact, arcname=artifact.name)
            payload = buffer.getvalue()
        return Response(
            content=payload,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="audit-package.zip"',
                "X-Content-SHA256": sha256(payload).hexdigest(),
            },
        )

    return app
