# SemanticaAdapter README Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the README so it first explains the Agent-governance problems addressed by introducing Semantica, then documents integrated modules, the decision flow, the banking example, operation, and limitations.

**Architecture:** This is a documentation-only change. `README.md` will use a problem-driven narrative and will derive all technical claims from the current source, integration tests, and `docs/capability-matrix.md`; no Python behavior or public interface changes are included.

**Tech Stack:** Markdown, Mermaid, Semantica 0.6.6, existing Python/pytest verification commands.

## Global Constraints

- The first paragraph must answer why the project introduces Semantica.
- Business value and governance problems must appear before adapter/interface architecture.
- Integrated and deferred Semantica modules must be clearly separated.
- Do not claim direct integration of `semantica.kg`, `ingest`, `semantic_extract`, visualization, or vector-store capabilities.
- Explain that audit explanations are system-level rule/evidence explanations, not private model chain-of-thought.
- Preserve the distinction between a runnable prototype and a bank-production compliance system.
- The workspace is not a Git repository; do not add commit steps or initialize Git.

---

### Task 1: Rewrite README Around the Semantica Adoption Problem

**Files:**
- Modify: `README.md`
- Reference: `docs/superpowers/specs/2026-08-25-readme-rewrite-design.md`
- Reference: `docs/capability-matrix.md`
- Reference: `examples/amount_reconciliation.py`

**Interfaces:**
- Consumes: the existing SemanticaAdapter behavior and Semantica 0.6.6 integration status.
- Produces: a self-contained project introduction for business, compliance, and engineering readers.

- [ ] **Step 1: Replace the opening and problem statement**

Begin with this meaning, expressed naturally rather than as an interface disclaimer:

```text
Many Agents can produce an audit result but cannot later show which versioned rules,
evidence, source data, and approval steps supported that result. This project introduces
Semantica to bind those elements to the Agent and preserve the full decision context in
an explainable, traceable graph for human review and regulatory inspection.
```

Add four explicit problem areas: Agent-rule binding, invisible decision basis, fragmented approval/exception records, and incomplete regulatory evidence chains.

- [ ] **Step 2: Add the complete governance flow**

Include a Mermaid flow containing these exact stages:

```text
Business request and evidence
→ versioned Agent profile
→ ontology validation
→ deterministic rule reasoning
→ decision and system-level explanation
→ human approval / policy exception
→ Context Graph and provenance
→ JSON/RDF/PROV-O audit package
→ offline integrity verification / regulatory review
```

- [ ] **Step 3: Document integrated Semantica modules**

Add a table with these exact status statements:

| Module | Status | Project use |
|---|---|---|
| `semantica.context` | Integrated and tested | Context Graph, decisions, approvals, exceptions |
| `semantica.reasoning` | Integrated and tested | Forward-chaining deterministic rules and explanation steps |
| `semantica.provenance` | Integrated and tested | Evidence/profile/decision sources and PROV-O |
| `semantica.ontology` | Integrated and tested with v1 mapping | Ontology structure and input-type validation |
| `semantica.export` | Integrated and tested | JSON and RDF/Turtle export |
| `semantica.kg` | Not directly exposed | Advanced centrality, community, path, and link-prediction analysis is deferred |

After the table, separately list deferred ingestion, semantic extraction, normalization, vector search, visualization, temporal intelligence, and multi-Agent sharing.

- [ ] **Step 4: Expand the amount-reconciliation example**

Explain the actual fixture values and lifecycle:

```text
voucher declared amount = 10,100
authoritative ledger amount = 10,000
rule fact = amount_mismatch
rule conclusion = manual_review
initial decision status = pending_approval
final status after risk-manager approval = approved
```

List what a reviewer can recover: Agent/profile version, rule-set version, ontology version, evidence IDs and hashes, source URIs, matched rule, public reasoning summary, approval identity/method, exception records, backend/version, and artifact digests.

- [ ] **Step 5: Add operation and project-layout sections**

Keep these verified commands:

```bash
uv sync
.venv/bin/python examples/amount_reconciliation.py
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests examples
```

Document `src/semantica_adapter/{api,domain,ports,services,adapters}` and explain `legacy/auditgraph` is retained but excluded from the wheel.

- [ ] **Step 6: Preserve precise integrity and production limitations**

State that colocated hashes prove package self-consistency, while resistance to coordinated rewriting requires passing an externally stored or signed `trusted_chain_head`. List SSO/RBAC, segregation of duties, transactional persistence, WORM/HSM, trusted timestamps, data masking, monitoring, and regulatory retention as production work.

### Task 2: Validate README Claims Against the Repository

**Files:**
- Verify: `README.md`
- Verify: `docs/capability-matrix.md`
- Verify: `src/semantica_adapter/adapters/semantica/backend.py`

**Interfaces:**
- Consumes: the rewritten README.
- Produces: evidence that headings, commands, module claims, and test counts match the project.

- [ ] **Step 1: Scan for incomplete language and forbidden overclaims**

Run:

```bash
rg -n "TBD|TODO|完整复现|已经集成.*semantica\.kg|私有思维链" README.md
```

Expected: no incomplete placeholders or claims that `semantica.kg` is integrated.

- [ ] **Step 2: Check required sections**

Run:

```bash
rg -n "为什么引入 Semantica|已集成的 Semantica 模块|金额核对|快速开始|项目结构|生产边界" README.md
```

Expected: every required section is present.

- [ ] **Step 3: Run documentation-adjacent project verification**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests examples
```

Expected: all existing tests pass and compilation exits with status 0.

- [ ] **Step 4: Manually compare module table with capability matrix**

Confirm `context`, `reasoning`, `provenance`, `ontology`, and `export` are integrated; confirm `kg`, `ingest`, `semantic_extract`, `normalize`, `vector_store`, and visualization remain deferred. Correct README text if any status differs.
