# SemanticaAdapter HTTP Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make company Agents use SemanticaAdapter through an authenticated HTTP API while keeping Semantica 0.6.6 as a server-only, replaceable backend.

**Architecture:** Add a wire-codec boundary, a FastAPI application over `AgentGovernanceService`, and an `httpx` client. Preserve the existing in-process API and `GovernanceBackend`; move Semantica to optional extras and remove the local source override.

**Tech Stack:** Python 3.11+, dataclasses, FastAPI, httpx, Uvicorn, Semantica 0.6.6, pytest.

## Global Constraints

- Company Agents must not import or install Semantica.
- Only the `semantica` and `server` extras depend on `semantica==0.6.6`.
- All `/v1` routes require `X-API-Key`; `/health` remains unauthenticated.
- Existing fail-closed rules, approval authorization, exception constraints and audit-package integrity must remain unchanged.
- `short-term-memory` remains a separate service and repository.

---

### Task 1: Packaging and import isolation

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/semantica_adapter/api/factory.py`
- Modify: `.gitignore`
- Test: `tests/unit/test_package.py`

**Interfaces:**
- Produces: base install with `httpx`; extras named `semantica` and `server`; lazy `create_local_semantica_service()`.

- [x] Add tests that parse `pyproject.toml`, reject `tool.uv.sources`, require `pythonpath = ["src"]`, and verify Semantica appears only in optional extras.
- [x] Add a subprocess import-isolation test whose meta-path finder raises if any `semantica` module is imported.
- [x] Run the focused tests and confirm they fail because the path override and eager import still exist.
- [x] Move Semantica imports inside `create_local_semantica_service`, update dependency groups/extras, and ignore `*.ses` plus local environment files.
- [x] Regenerate `uv.lock` without the local source override.
- [x] Run `pytest tests/unit/test_package.py -q` and confirm it passes.

### Task 2: Stable JSON wire codec

**Files:**
- Create: `src/semantica_adapter/http/__init__.py`
- Create: `src/semantica_adapter/http/wire.py`
- Test: `tests/unit/test_http_wire.py`

**Interfaces:**
- Produces: `to_wire(value) -> JSONValue`, `agent_profile_from_wire`, `audit_request_from_wire`, `audit_session_from_wire`, `decision_from_wire`, `approval_from_wire`, `exception_from_wire`, `trace_from_wire`.

- [x] Write round-trip tests for profile, evidence/request/session, rule evaluation/decision, approval/exception and trace models.
- [x] Run the focused test and confirm collection fails because `semantica_adapter.http.wire` does not exist.
- [x] Implement recursive encoding for dataclasses, `datetime`, `Path`, `Enum`, mappings and sequences; implement explicit decoders so untrusted JSON cannot select arbitrary classes.
- [x] Run `pytest tests/unit/test_http_wire.py -q` and confirm all codec tests pass.

### Task 3: Authenticated FastAPI governance service

**Files:**
- Create: `src/semantica_adapter/http/app.py`
- Create: `src/semantica_adapter/http/runtime.py`
- Test: `tests/integration/test_http_app.py`

**Interfaces:**
- Consumes: `AgentGovernanceService`, wire codec.
- Produces: `create_app(service, api_key) -> FastAPI`, `create_runtime_app() -> FastAPI`, `main() -> None` and the `/health` plus `/v1` routes in the design.

- [x] Write a TestClient test proving `/v1` rejects missing and invalid API keys.
- [x] Run it and confirm import fails because the app module does not exist.
- [x] Implement constant-time API-key verification and stable error responses.
- [x] Add a full lifecycle test: register profile, start audit, evaluate mismatch, record pending decision, record exception, approve, query trace.
- [x] Run it and confirm the lifecycle routes are missing.
- [x] Implement the lifecycle routes using only public `AgentGovernanceService` methods.
- [x] Add an audit-package test that opens the returned ZIP and verifies JSON, Turtle, manifest and audit-chain entries plus `X-Content-SHA256`.
- [x] Implement temporary package generation, ZIP streaming and cleanup.
- [x] Add runtime configuration tests for missing API key and malformed authorized-actor JSON; implement environment parsing and Uvicorn entry point.
- [x] Run `pytest tests/integration/test_http_app.py -q` and confirm all service tests pass.

### Task 4: Agent-side HTTP client

**Files:**
- Create: `src/semantica_adapter/http/client.py`
- Modify: `src/semantica_adapter/http/__init__.py`
- Test: `tests/unit/test_http_client.py`

**Interfaces:**
- Produces: `SemanticaHttpClient(base_url, api_key, timeout=10.0, client=None)` with `health_check`, `register_agent`, `start_audit`, `evaluate`, `record_decision`, `submit_approval`, `record_exception`, `get_audit_trace`, and `download_audit_package`.

- [x] Write MockTransport tests for authenticated requests and domain-model decoding.
- [x] Run them and confirm import fails because the client does not exist.
- [x] Implement request construction, URL normalization, ownership-aware close/context-manager behavior and response decoding.
- [x] Add tests for 401/403/422/502 mapping and transport/invalid-JSON failures.
- [x] Implement stable exception mapping without exposing `httpx` exceptions.
- [x] Add a package-download test with matching and mismatching SHA-256 headers.
- [x] Implement verified atomic package download.
- [x] Run `pytest tests/unit/test_http_client.py -q` and confirm all client tests pass.

### Task 5: Documentation, source location guide and release verification

**Files:**
- Modify: `README.md`
- Modify: `docs/capability-matrix.md`
- Create: `docs/使用与功能定位.md`
- Modify: `THIRD_PARTY_NOTICES.md` if dependency notices require clarification.

**Interfaces:**
- Produces: teacher-facing installation, deployment, usage, feature-to-source, testing and repository guidance.

- [x] Update README architecture and quick start so HTTP is the recommended path and local SDK is explicitly a prototype/development option.
- [x] Document server installation, environment variables, startup, Python client example and curl examples without real credentials.
- [x] Add a feature-location table mapping every implemented capability to entry point, service, backend and test files.
- [x] Explain why `short-term-memory` remains a separate service and show the combined Agent call sequence.
- [x] Search documentation for stale claims about required `../semantica-main` and remove them.
- [x] Run the complete unit, contract and integration test suite.
- [x] Run `python -m compileall -q src tests examples`.
- [x] Build wheel/sdist, list their contents, and verify no `legacy`, generated outputs, `.ses`, cache or local Semantica source is packaged.
- [x] Test installation in a clean temporary virtual environment using the built wheel for the base client and the server extra.

