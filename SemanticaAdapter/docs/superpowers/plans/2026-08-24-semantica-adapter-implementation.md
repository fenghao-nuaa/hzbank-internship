# SemanticaAdapter Implementation Plan

> 历史计划：其中的本地 `../semantica-main` 依赖方式已经由 `2026-08-26-http-service.md` 取代。当前安装、部署和验收请以 README 与 `docs/使用与功能定位.md` 为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the AuditGraph reimplementation with a `src`-layout adapter package that gives company agents a stable governance API backed by Semantica 0.6.6.

**Architecture:** Company agents call `AgentGovernanceService`, which depends only on provider-neutral domain models and the `GovernanceBackend` protocol. `SemanticaAdapter` implements that protocol by mapping decisions, evidence, approvals, policy exceptions, provenance, ontology checks, and exports to Semantica public APIs; a fake backend runs the same contract tests to prove replaceability.

**Tech Stack:** Python 3.11+, Semantica 0.6.6, dataclasses, typing.Protocol, pytest 8, uv, JSON and RDF/PROV-O exports.

## Global Constraints

- Project directory becomes `semantica-adapter`; distribution name is `semantica-adapter`; import package is `semantica_adapter`.
- Use the standard `src/semantica_adapter` layout.
- Company agent examples must not import `semantica.*`.
- Pin and report Semantica 0.6.6 as the initial compatibility target.
- Use Semantica public APIs; do not copy or reimplement Semantica algorithms.
- Keep Semantica objects out of public method parameters and return values.
- Map approvals to `ApprovalChain` and exceptions to `PolicyException`. Compatibility decision: Semantica 0.6.6's public recorder paths cannot preserve caller-owned IDs on in-memory `ContextGraph` (and approval assumes `execute_query()`), so v1 persists the mapped public models with public `ContextGraph.add_node/add_edge`; no private Semantica API or algorithm copy is used.
- Implement only the v1 scope: context, decision, reasoning/policy checks, provenance, ontology validation, approval/exception recording, and JSON/RDF export.
- Missing required evidence, missing decision fields, ontology failure, or decision-critical conflict must fail closed to `manual_review`.
- Preserve the current AuditGraph implementation under `legacy/auditgraph` until migration acceptance; exclude it from the wheel.
- The workspace is not currently a Git repository. Do not initialize Git without explicit user approval; use test checkpoints instead of commit steps.

---

## Target File Map

```text
semantica-adapter/
├── pyproject.toml                    # package metadata, local Semantica source, pytest config
├── README.md                         # installation, architecture, supported capabilities
├── THIRD_PARTY_NOTICES.md            # Semantica MIT attribution
├── src/semantica_adapter/
│   ├── __init__.py                   # stable public exports
│   ├── api/service.py                # AgentGovernanceService facade
│   ├── domain/models.py              # provider-neutral immutable records
│   ├── domain/errors.py              # stable public exception hierarchy
│   ├── ports/backend.py              # GovernanceBackend protocol
│   ├── ports/profiles.py             # AgentProfileRepository protocol
│   ├── ports/approvals.py            # ApprovalWorkflowPort protocol
│   ├── services/governance.py        # fail-closed orchestration and state transitions
│   ├── adapters/semantica/backend.py # Semantica 0.6.6 implementation
│   ├── adapters/semantica/mapping.py # domain ↔ Semantica conversion only
│   ├── adapters/semantica/config.py  # backend configuration and version checks
│   ├── adapters/memory/backend.py    # fake contract backend
│   ├── adapters/memory/profiles.py   # in-memory profile versions
│   └── adapters/memory/approvals.py  # controlled test approval workflow
├── tests/unit/
├── tests/contract/
├── tests/integration/
├── examples/amount_reconciliation.py
├── docs/superpowers/
└── legacy/auditgraph/
```

---

### Task 1: Rename and Package the Project Safely

**Files:**
- Rename: `auditgraph-main/` → `semantica-adapter/`
- Move: `semantica-adapter/auditgraph/` → `semantica-adapter/legacy/auditgraph/`
- Move: `semantica-adapter/tests/` → `semantica-adapter/legacy/tests/`
- Modify: `semantica-adapter/pyproject.toml`
- Modify: `semantica-adapter/.gitignore`
- Create: `semantica-adapter/THIRD_PARTY_NOTICES.md`
- Create: `semantica-adapter/src/semantica_adapter/__init__.py`
- Test: `semantica-adapter/tests/unit/test_package.py`

**Interfaces:**
- Consumes: sibling source tree `../semantica-main`, Semantica version `0.6.6`.
- Produces: importable `semantica_adapter` package and local editable Semantica dependency.

- [ ] **Step 1: Rename the project and preserve legacy code**

Run from `/Users/fenghao/PycharmProjects/semantica`:

```bash
mv auditgraph-main semantica-adapter
mkdir -p semantica-adapter/legacy
mv semantica-adapter/auditgraph semantica-adapter/legacy/auditgraph
mv semantica-adapter/tests semantica-adapter/legacy/tests
mkdir -p semantica-adapter/src/semantica_adapter semantica-adapter/tests/unit
```

Expected: the design and plan move with the renamed project; old code remains under `legacy/`.

- [ ] **Step 2: Write the failing package test**

Create `tests/unit/test_package.py`:

```python
def test_package_exposes_version_and_semantica_target() -> None:
    import semantica_adapter

    assert semantica_adapter.__version__ == "0.1.0"
    assert semantica_adapter.SEMANTICA_COMPAT_VERSION == "0.6.6"
```

- [ ] **Step 3: Run the test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_package.py -q
```

Expected: FAIL because the `src` package is not configured and constants do not exist.

- [ ] **Step 4: Replace package configuration**

Set `pyproject.toml` to:

```toml
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "semantica-adapter"
version = "0.1.0"
description = "Provider-neutral agent governance interfaces backed by Semantica"
readme = "README.md"
requires-python = ">=3.11"
license = { text = "MIT" }
dependencies = ["semantica==0.6.6"]

[dependency-groups]
dev = ["pytest>=8.0,<9.0"]

[tool.uv.sources]
semantica = { path = "../semantica-main", editable = true }

[tool.hatch.build.targets.wheel]
packages = ["src/semantica_adapter"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra"
pythonpath = ["src", "../semantica-main"]
```

Create `src/semantica_adapter/__init__.py`:

```python
"""Stable agent-governance interface backed by pluggable providers."""

__version__ = "0.1.0"
SEMANTICA_COMPAT_VERSION = "0.6.6"
```

Add `.venv`, generated audit output, caches, and `legacy/` bytecode to `.gitignore` without ignoring `legacy/` source files.

- [ ] **Step 5: Add third-party attribution**

Create `THIRD_PARTY_NOTICES.md` stating that Semantica 0.6.6 is an MIT-licensed dependency, identifying `https://github.com/semantica-agi/semantica`, and pointing to `../semantica-main/LICENSE`. Do not claim the adapter is authored by Semantica.

- [ ] **Step 6: Synchronize and verify**

Run:

```bash
uv sync
.venv/bin/python -m pytest tests/unit/test_package.py -q
```

Expected: `1 passed` and `python -c "import semantica; print(semantica.__version__)"` prints `0.6.6`.

---

### Task 2: Define Stable Domain Models and Errors

**Files:**
- Create: `src/semantica_adapter/domain/__init__.py`
- Create: `src/semantica_adapter/domain/models.py`
- Create: `src/semantica_adapter/domain/errors.py`
- Test: `tests/unit/test_domain_models.py`

**Interfaces:**
- Consumes: Python standard library only.
- Produces: `AgentProfile`, `EvidenceRef`, `AuditRequest`, `AuditSession`, `RuleEvaluation`, `DecisionRecord`, `ApprovalRecord`, `PolicyExceptionRecord`, `AuditTrace`, `AuditExport`, `AuditStatus`.

- [ ] **Step 1: Write model validation tests**

Create tests that instantiate each record and assert:

```python
from semantica_adapter.domain.models import AgentProfile, AuditStatus, EvidenceRef


def test_profile_requires_versioned_governance_bindings() -> None:
    profile = AgentProfile(
        agent_id="amount-checker",
        name="Amount Checker",
        purpose="Reconcile declared and ledger amounts",
        profile_version="1.0",
        rule_set_id="amount-rules",
        rule_set_version="2026.08",
        ontology_id="banking",
        ontology_version="1.0",
        allowed_source_types=("ledger", "voucher"),
        approval_policy="manual_on_review",
    )
    assert profile.agent_id == "amount-checker"


def test_evidence_requires_sha256_hex_digest() -> None:
    import pytest

    with pytest.raises(ValueError, match="SHA-256"):
        EvidenceRef("e-1", "ledger", "ledger://entry/1", "not-a-hash")


def test_audit_status_has_manual_review_and_pending_approval() -> None:
    assert AuditStatus.MANUAL_REVIEW.value == "manual_review"
    assert AuditStatus.PENDING_APPROVAL.value == "pending_approval"
```

- [ ] **Step 2: Run tests and verify failure**

Run `pytest tests/unit/test_domain_models.py -q`.

Expected: FAIL with missing modules/classes.

- [ ] **Step 3: Implement focused immutable dataclasses**

Implement `AuditStatus` as a string enum. Implement frozen, slotted dataclasses with tuple/default-factory fields and `__post_init__` validation. Required constraints:

```python
class AuditStatus(str, Enum):
    OPEN = "open"
    EVALUATED = "evaluated"
    MANUAL_REVIEW = "manual_review"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    source_type: str
    source_uri: str
    content_hash: str
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.content_hash) != 64 or any(ch not in hexdigits for ch in self.content_hash.lower()):
            raise ValueError("content_hash must be a SHA-256 hexadecimal digest")
```

Use the exact field lists in design sections 7.1–7.7 with these concrete additions needed by the adapter:

- `DecisionRecord`: `decision_id`, `audit_id`, `agent_id`, `profile_version`, `category`, `scenario`, `outcome`, `reasoning_summary`, `confidence`, `evidence_ids`, `rule_evaluation`, `backend_name`, `backend_version`, `created_at`, `status`.
- `ApprovalRecord`: `approval_id`, `decision_id`, `approver_id`, `approver_role`, `action`, `approval_method`, `approval_context`, `timestamp`, `metadata`.
- `PolicyExceptionRecord`: `exception_id`, `decision_id`, `policy_id`, `reason`, `approver_id`, `approval_method`, `approved_at`, `justification`, `metadata`.
- `AuditTrace`: `decision_id`, `agent_id`, `profile_version`, `rule_set_id`, `rule_set_version`, `evidence`, `nodes`, `approvals`, `exceptions`.
- `AuditExport`: `decision_id`, `format`, `path`, `sha256`, `generated_at`.

`DecisionRecord.reasoning_summary` must be public system reasoning, never a private chain-of-thought field.

- [ ] **Step 4: Implement stable exceptions**

Create:

```python
class SemanticaAdapterError(RuntimeError):
    pass
class ConfigurationError(SemanticaAdapterError):
    pass
class UnsupportedCapabilityError(SemanticaAdapterError):
    pass
class ValidationError(SemanticaAdapterError):
    pass
class EvidenceError(SemanticaAdapterError):
    pass
class BackendError(SemanticaAdapterError):
    pass
class AuditIntegrityError(SemanticaAdapterError):
    pass
class ApprovalRequiredError(SemanticaAdapterError):
    pass
```

- [ ] **Step 5: Run model tests**

Run `pytest tests/unit/test_domain_models.py -q`.

Expected: all tests pass.

---

### Task 3: Define Backend, Profile, and Approval Contracts

**Files:**
- Create: `src/semantica_adapter/ports/__init__.py`
- Create: `src/semantica_adapter/ports/backend.py`
- Create: `src/semantica_adapter/ports/profiles.py`
- Create: `src/semantica_adapter/ports/approvals.py`
- Create: `src/semantica_adapter/adapters/memory/backend.py`
- Create: `src/semantica_adapter/adapters/memory/profiles.py`
- Create: `src/semantica_adapter/adapters/memory/approvals.py`
- Test: `tests/contract/test_backend_contract.py`
- Test: `tests/unit/test_memory_ports.py`

**Interfaces:**
- Consumes: Task 2 domain models.
- Produces: runtime-checkable `GovernanceBackend`, `AgentProfileRepository`, `ApprovalWorkflowPort`, and fake implementations.

- [ ] **Step 1: Write failing protocol and repository tests**

Test that the memory backend satisfies `isinstance(backend, GovernanceBackend)`, profile versions are immutable, an unknown profile raises `KeyError`, and an approval cannot be fabricated by an Agent actor.

- [ ] **Step 2: Define the protocols**

`GovernanceBackend` must expose these exact methods:

```python
@runtime_checkable
class GovernanceBackend(Protocol):
    name: str
    version: str
    def capabilities(self) -> frozenset[str]:
        raise NotImplementedError
    def health_check(self) -> Mapping[str, Any]:
        raise NotImplementedError
    def record_profile_snapshot(self, audit_id: str, profile: AgentProfile) -> str:
        raise NotImplementedError
    def record_evidence(self, audit_id: str, evidence: EvidenceRef) -> str:
        raise NotImplementedError
    def validate_ontology(self, audit_id: str, profile: AgentProfile, inputs: Mapping[str, Any]) -> tuple[str, ...]:
        raise NotImplementedError
    def evaluate_rules(self, audit_id: str, profile: AgentProfile, inputs: Mapping[str, Any]) -> RuleEvaluation:
        raise NotImplementedError
    def record_decision(self, decision: DecisionRecord) -> str:
        raise NotImplementedError
    def record_approval(self, approval: ApprovalRecord) -> str:
        raise NotImplementedError
    def record_exception(self, exception: PolicyExceptionRecord) -> str:
        raise NotImplementedError
    def trace_decision(self, decision_id: str) -> AuditTrace:
        raise NotImplementedError
    def export_decision(self, decision_id: str, output_dir: Path, format: str) -> AuditExport:
        raise NotImplementedError
```

`AgentProfileRepository` exposes `save(profile)` and `get(agent_id, profile_version=None)`. `ApprovalWorkflowPort` exposes `authorize(approval)` and must not perform Semantica graph persistence.

- [ ] **Step 3: Implement memory ports**

Implement dictionaries keyed by `(agent_id, profile_version)` and IDs. `MemoryApprovalWorkflow` accepts an explicit set of authorized `(actor_id, role)` pairs and returns false for unknown actors.

- [ ] **Step 4: Implement the fake backend**

The fake stores every call as domain objects, returns deterministic IDs, makes missing keys from `profile.metadata["required_fields"]` appear in `RuleEvaluation.missing_fields`, and creates JSON exports with a SHA-256 hash. It must not import Semantica.

- [ ] **Step 5: Add backend contract tests**

Create a reusable `backend` pytest fixture parameter and assert that evidence, decision, approval, exception, trace, and export round-trip through the backend interface.

- [ ] **Step 6: Run contract and unit tests**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_memory_ports.py tests/contract/test_backend_contract.py -q
```

Expected: all tests pass against the fake backend.

---

### Task 4: Implement Semantica Configuration and Mapping

**Files:**
- Create: `src/semantica_adapter/adapters/semantica/__init__.py`
- Create: `src/semantica_adapter/adapters/semantica/config.py`
- Create: `src/semantica_adapter/adapters/semantica/mapping.py`
- Test: `tests/unit/test_semantica_mapping.py`
- Test: `tests/unit/test_semantica_config.py`

**Interfaces:**
- Consumes: Task 2 models; installed `semantica==0.6.6`.
- Produces: `SemanticaConfig`, version guard, and conversion helpers for decisions, approvals, and exceptions.

- [ ] **Step 1: Write failing configuration tests**

Assert that default config uses an in-memory context graph, accepts an optional provenance SQLite path, rejects unsupported installed Semantica versions, and never includes credentials in `repr(config)`.

- [ ] **Step 2: Implement configuration**

Use:

```python
@dataclass(frozen=True, slots=True)
class SemanticaConfig:
    provenance_storage_path: Path | None = None
    advanced_analytics: bool = False
    strict_version: bool = True

    def verify_version(self) -> str:
        import semantica
        if self.strict_version and semantica.__version__ != "0.6.6":
            raise ConfigurationError(
                f"Semantica 0.6.6 required, found {semantica.__version__}"
            )
        return semantica.__version__
```

- [ ] **Step 3: Write failing mapping tests**

Assert `DecisionRecord` maps to Semantica `Decision`, `ApprovalRecord` maps to `ApprovalChain`, and `PolicyExceptionRecord` maps to Semantica `PolicyException` without losing IDs, timestamps, actor identity, policy ID, or metadata.

- [ ] **Step 4: Implement mapping functions**

Create exact functions:

```python
from semantica.context import ApprovalChain, Decision, PolicyException


def to_semantica_decision(record: DecisionRecord) -> Decision:
    return Decision(
        decision_id=record.decision_id,
        category=record.category,
        scenario=record.scenario,
        reasoning=record.reasoning_summary,
        outcome=record.outcome,
        confidence=record.confidence,
        timestamp=record.created_at,
        decision_maker=record.agent_id,
        metadata={
            "audit_id": record.audit_id,
            "profile_version": record.profile_version,
            "evidence_ids": list(record.evidence_ids),
            "backend_name": record.backend_name,
            "backend_version": record.backend_version,
            "status": record.status.value,
        },
    )


def to_semantica_approval(record: ApprovalRecord) -> ApprovalChain:
    return ApprovalChain(
        approval_id=record.approval_id,
        decision_id=record.decision_id,
        approver=record.approver_id,
        approval_method=record.approval_method,
        approval_context=record.approval_context,
        timestamp=record.timestamp,
        metadata={
            **dict(record.metadata),
            "approver_role": record.approver_role,
            "action": record.action,
        },
    )


def to_semantica_exception(record: PolicyExceptionRecord) -> PolicyException:
    return PolicyException(
        exception_id=record.exception_id,
        decision_id=record.decision_id,
        policy_id=record.policy_id,
        reason=record.reason,
        approver=record.approver_id,
        approval_timestamp=record.approved_at,
        justification=record.justification,
        metadata={**dict(record.metadata), "approval_method": record.approval_method},
    )


def normalize_trace(
    raw: Sequence[Mapping[str, Any]],
    *,
    decision_id: str,
    agent_id: str,
    profile_version: str,
    rule_set_id: str,
    rule_set_version: str,
) -> AuditTrace:
    return AuditTrace(
        decision_id=decision_id,
        agent_id=agent_id,
        profile_version=profile_version,
        rule_set_id=rule_set_id,
        rule_set_version=rule_set_version,
        evidence=(),
        nodes=tuple(dict(item) for item in raw),
        approvals=(),
        exceptions=(),
    )
```

Put extra bank fields in Semantica metadata instead of monkey-patching Semantica classes.

- [ ] **Step 5: Run mapping tests**

Run `pytest tests/unit/test_semantica_config.py tests/unit/test_semantica_mapping.py -q`.

Expected: all pass with Semantica 0.6.6.

---

### Task 5: Implement the Semantica Governance Backend

**Files:**
- Create: `src/semantica_adapter/adapters/semantica/backend.py`
- Modify: `tests/contract/test_backend_contract.py`
- Test: `tests/integration/test_semantica_backend.py`

**Interfaces:**
- Consumes: `GovernanceBackend`, Task 4 config/mappers, Semantica `ContextGraph`, `DecisionRecorder`, `ProvenanceManager`, `OntologyEngine`, JSON/RDF exporters.
- Produces: `SemanticaAdapter(GovernanceBackend)`.

- [ ] **Step 1: Add the Semantica backend to contract tests**

Parametrize the contract fixture with `FakeGovernanceBackend()` and `SemanticaAdapter(SemanticaConfig())`. Expected initial failure: `SemanticaAdapter` missing.

- [ ] **Step 2: Initialize only required Semantica components**

Constructor behavior:

```python
self.version = config.verify_version()
self.name = "semantica"
self.graph = ContextGraph(advanced_analytics=config.advanced_analytics)
self.provenance = ProvenanceManager(
    storage_path=str(config.provenance_storage_path) if config.provenance_storage_path else None
)
self.recorder = DecisionRecorder(self.graph, provenance_manager=self.provenance)
self.policy_engine = PolicyEngine(self.graph)
```

Avoid initializing embeddings, LLM clients, or external stores.

- [ ] **Step 3: Implement profile and evidence recording**

Store the profile snapshot as a graph node with a versioned ID and call `ProvenanceManager.track_entity()` for every profile and evidence record. Add graph edges from audit session to profile/evidence.

- [ ] **Step 4: Implement policy and ontology evaluation**

Use Semantica policy checks for configured rules. Return stable `RuleEvaluation`. Before returning, calculate `missing_fields` from the profile's required-field declaration; surface Semantica ontology validation errors as a tuple rather than swallowing them.

- [ ] **Step 5: Implement decision recording**

Call the public `ContextGraph.record_decision()` or `DecisionRecorder.record_decision()` with evidence IDs, profile/rule versions, audit ID, backend version, and public reasoning summary in metadata. Add causal/evidence relationships using Semantica graph APIs.

- [ ] **Step 6: Implement existing approval and exception capabilities**

Use `DecisionRecorder.record_approval_chain()` for `ApprovalRecord` and `DecisionRecorder.record_exception()` or `PolicyEngine.record_exception()` for `PolicyExceptionRecord`. Do not create custom approval graph nodes in the adapter.

- [ ] **Step 7: Implement trace and exports**

Normalize `ContextGraph.trace_decision_chain()` plus provenance lineage into `AuditTrace`. Export provider-neutral payloads through Semantica JSON and RDF exporters, then compute SHA-256 hashes and return `AuditExport`.

- [ ] **Step 8: Convert exceptions**

Wrap Semantica validation exceptions as `ValidationError`, unsupported format/capability as `UnsupportedCapabilityError`, and unexpected backend failures as `BackendError` while preserving the original exception with Python exception chaining.

- [ ] **Step 9: Run backend contract and integration tests**

Run:

```bash
.venv/bin/python -m pytest tests/contract/test_backend_contract.py tests/integration/test_semantica_backend.py -q
```

Expected: fake and Semantica implementations pass the same contract tests.

---

### Task 6: Implement Fail-Closed Governance Orchestration

**Files:**
- Create: `src/semantica_adapter/services/__init__.py`
- Create: `src/semantica_adapter/services/governance.py`
- Create: `src/semantica_adapter/api/__init__.py`
- Create: `src/semantica_adapter/api/service.py`
- Modify: `src/semantica_adapter/__init__.py`
- Test: `tests/unit/test_governance_service.py`

**Interfaces:**
- Consumes: backend/profile/approval ports and all domain models.
- Produces: public `AgentGovernanceService` facade.

- [ ] **Step 1: Write failing service tests**

Cover these exact behaviors using fixtures that construct a registered profile, `FakeGovernanceBackend`, `MemoryAgentProfileRepository`, and `MemoryApprovalWorkflow`:

```python
def test_missing_required_field_forces_manual_review(service, audit_request) -> None:
    audit = service.start_audit(audit_request)
    evaluated = service.evaluate(audit.audit_id)
    assert evaluated.status is AuditStatus.MANUAL_REVIEW
    assert evaluated.rule_evaluation.missing_fields == ("ledger_amount",)


def test_required_human_approval_stays_pending(service, complete_request) -> None:
    audit = service.start_audit(complete_request)
    service.evaluate(audit.audit_id)
    decision = service.record_decision(
        audit.audit_id,
        proposed_outcome="matched",
        reasoning_summary="Declared and ledger amounts match",
        confidence=1.0,
    )
    assert decision.status is AuditStatus.PENDING_APPROVAL


def test_unauthorized_actor_cannot_approve(service, pending_decision) -> None:
    approval = ApprovalRecord(
        approval_id="approval-1",
        decision_id=pending_decision.decision_id,
        approver_id="agent-process",
        approver_role="agent",
        action="approve",
        approval_method="system",
        approval_context="attempted self approval",
    )
    with pytest.raises(ApprovalRequiredError):
        service.submit_approval(approval)
```

Add separate tests named `test_ontology_error_forces_manual_review`, `test_conflict_forces_manual_review`, and `test_agent_code_uses_same_api_with_fake_or_semantica_backend`; assert respectively `MANUAL_REVIEW`, `MANUAL_REVIEW`, and identical domain result types from both backends.

- [ ] **Step 2: Implement profile registration and audit start**

`register_agent(profile)` saves an immutable version. `start_audit(request)` resolves the latest profile, creates an audit ID, records a profile snapshot and each evidence item through the backend, and returns an `AuditSession(status=OPEN)`.

- [ ] **Step 3: Implement deterministic evaluation**

`evaluate(audit_id)` runs ontology validation and rule evaluation. If validation errors, missing fields, or conflicts exist, return an updated session with `MANUAL_REVIEW`; otherwise use `EVALUATED`.

- [ ] **Step 4: Implement decision state transitions**

`record_decision(audit_id, proposed_outcome, reasoning_summary, confidence)` must reject calls before evaluation. It overrides unsafe automatic outcomes to `manual_review`. For `approval_policy in {"always", "manual_on_review"}` when applicable, set `PENDING_APPROVAL`; otherwise persist the final decision through the backend.

- [ ] **Step 5: Implement approval and exception operations**

`submit_approval()` first calls `ApprovalWorkflowPort.authorize()`, then calls backend `record_approval()`. Unauthorized callers raise `ApprovalRequiredError`. `record_exception()` follows the same authorization gate and calls the Semantica-backed exception method.

- [ ] **Step 6: Implement trace/export delegation**

`get_audit_trace()` and `export_audit()` delegate to the backend and return stable models. They do not expose backend objects.

- [ ] **Step 7: Export the stable public API**

`semantica_adapter.__init__` exports only `AgentGovernanceService`, domain request/result models, stable exceptions, and the compatibility version. It does not export Semantica classes.

- [ ] **Step 8: Run service tests**

Run `pytest tests/unit/test_governance_service.py -q`.

Expected: all fail-closed and approval-gate tests pass.

---

### Task 7: Build the Amount-Reconciliation Acceptance Example

**Files:**
- Create: `examples/amount_reconciliation.py`
- Create: `tests/integration/test_amount_reconciliation.py`

**Interfaces:**
- Consumes: the public API only.
- Produces: one runnable company-Agent integration example without direct Semantica imports.

- [ ] **Step 1: Write the acceptance test**

The example must register `amount-checker` with a versioned rule set, create two evidence records for a ledger and voucher, evaluate `declared_amount` versus `ledger_amount`, record a mismatch decision, require a human approval, and export JSON and RDF audit artifacts.

The test must assert:

```python
assert "semantica" not in agent_module_imports
assert decision.status is AuditStatus.PENDING_APPROVAL
assert trace.profile_version == "1.0"
assert trace.rule_set_version == "2026.08"
assert {item.evidence_id for item in trace.evidence} == {"ledger-1", "voucher-1"}
assert json_export.path.exists()
assert rdf_export.path.exists()
```

- [ ] **Step 2: Run the acceptance test to verify failure**

Run `pytest tests/integration/test_amount_reconciliation.py -q`.

Expected: FAIL until the example is implemented.

- [ ] **Step 3: Implement the example using only the stable API**

The example imports from `semantica_adapter`, constructs `AgentGovernanceService`, and receives the backend from a factory argument. It must not import `semantica`, `ContextGraph`, or `ProvenanceManager`.

- [ ] **Step 4: Run acceptance test**

Run `pytest tests/integration/test_amount_reconciliation.py -q`.

Expected: PASS and audit output contains profile, rules, evidence, decision, approval, provenance, backend name, and backend version.

---

### Task 8: Add Offline Integrity and Regression Coverage

**Files:**
- Create: `src/semantica_adapter/services/integrity.py`
- Test: `tests/integration/test_integrity.py`
- Migrate selected tests from: `legacy/tests/regression/test_pipeline_integrity.py`

**Interfaces:**
- Consumes: `AuditExport` and exported JSON/RDF files.
- Produces: `verify_export_package(output_dir) -> IntegrityResult`.

- [ ] **Step 1: Write tamper and rollback tests**

Cover JSON tampering, RDF tampering, missing artifact, graph digest mismatch, manifest/artifact-list mismatch, and export failure rollback. Preserve the existing attack case where deleting manifest entries and corresponding files must still fail verification.

- [ ] **Step 2: Implement a detached manifest**

Manifest fields are `schema_version`, `decision_id`, `backend_name`, `backend_version`, `graph_sha256`, `artifacts`, and `audit_chain_head`. Hash JSON and RDF; bind the required artifact names in the manifest and in the recorded export entry.

- [ ] **Step 3: Implement offline verification**

The verifier treats the chain-bound artifact list as authoritative, requires JSON and RDF, compares top-level manifest fields to the chain-bound export entry, and reports structured error reasons.

- [ ] **Step 4: Keep publication failure-safe**

Render into a sibling temporary directory. Before publishing, back up existing target files; on any replacement failure, delete newly published files and restore backups.

- [ ] **Step 5: Run integrity tests**

Run `pytest tests/integration/test_integrity.py -q`.

Expected: valid packages pass; each mutation fails with the expected reason; publish failure leaves no partial package.

---

### Task 9: Documentation, Full Verification, and Legacy Boundary

**Files:**
- Rewrite: `README.md`
- Create: `docs/capability-matrix.md`
- Modify: `THIRD_PARTY_NOTICES.md`
- Test: all `tests/`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: documented v1 package with explicit supported/deferred capabilities.

- [ ] **Step 1: Rewrite README**

Document the project as an adapter, not a graph implementation. Include installation, the architecture boundary, amount-reconciliation example, supported Semantica modules, fail-closed semantics, approval/exception mapping, offline verification, and production limitations.

- [ ] **Step 2: Add capability matrix**

For each Semantica capability, label it `supported`, `mapped`, or `deferred`. V1 supported rows must include context decisions, policy checks, provenance, ontology validation, approval chain, policy exception, JSON export, and RDF/PROV-O export.

- [ ] **Step 3: Verify package contents**

Run:

```bash
uv build
unzip -l dist/semantica_adapter-0.1.0-py3-none-any.whl
```

Expected: wheel contains only `semantica_adapter` and metadata; it does not contain `legacy/auditgraph`.

- [ ] **Step 4: Run complete verification**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests examples
.venv/bin/python examples/amount_reconciliation.py
```

Expected: all tests pass, compilation succeeds, the example produces valid JSON/RDF/manifest artifacts, and offline verification returns valid.

- [ ] **Step 5: Report migration without deleting legacy source**

Report the renamed directory, new public API, Semantica version, tests executed, generated artifact formats, and remaining deferred capabilities. Keep `legacy/` until the user separately approves removal.
