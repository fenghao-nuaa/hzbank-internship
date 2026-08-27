# AuditGraph MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, locally runnable reproduction of Semantica's complete source-to-audit pipeline inside `auditgraph-main`.

**Architecture:** Keep Semantica's package-oriented layout and expose one focused public interface per stage. A synchronous `AuditPipeline` composes the stages, while dataclasses provide stable contracts between them and an append-only provenance manager binds all outputs into a verifiable audit chain.

**Tech Stack:** Python 3.11+, standard library, pytest; optional `rdflib` only for richer RDF handling, with deterministic Turtle export available without it.

## Global Constraints

- Keep the project structure and public naming close to Semantica.
- Implement the user-approved flow without adding a second framework.
- Runtime tests must not use the public internet, external databases, or remote LLMs.
- Use test-driven development for every behavior.
- The first approval implementation records approval and policy exceptions; it is not a bank-grade workflow engine.
- Explain observable inputs, rules, outputs, and provenance; never store hidden model chain-of-thought.

---

### Task 1: Project foundation and shared contracts

**Files:**
- Create: `pyproject.toml`
- Create: `auditgraph/__init__.py`
- Create: `auditgraph/core/models.py`
- Create: `tests/core/test_models.py`

**Interfaces:**
- Produces: `SourceDocument`, `Chunk`, `Entity`, `Relation`, `Event`, `Triplet`, `Conflict`, `Decision`, `Approval`, `PolicyException`, and `PipelineResult` dataclasses.

- [ ] **Step 1: Write the failing model validation tests**

```python
from auditgraph.core.models import Decision, SourceDocument

def test_source_document_requires_source_id():
    with pytest.raises(ValueError, match="source_id"):
        SourceDocument(source_id="", source_type="file", content="data")

def test_decision_rejects_invalid_confidence():
    with pytest.raises(ValueError, match="confidence"):
        Decision(category="credit", scenario="s", reasoning="r", outcome="review", confidence=1.1)
```

- [ ] **Step 2: Run `python -m pytest tests/core/test_models.py -q` and verify import failure**
- [ ] **Step 3: Implement immutable boundary dataclasses with ID generation, UTC timestamps, validation, and `to_dict()` serialization**
- [ ] **Step 4: Re-run the model tests and verify they pass**

### Task 2: Ingestion, parsing, normalization, splitting, and extraction

**Files:**
- Create: `auditgraph/ingest/{__init__,file_ingestor,web_ingestor,db_ingestor,api_ingestor}.py`
- Create: `auditgraph/parse/{__init__,document_parser}.py`
- Create: `auditgraph/normalize/{__init__,normalizers}.py`
- Create: `auditgraph/split/{__init__,splitter}.py`
- Create: `auditgraph/semantic_extract/{__init__,extractor}.py`
- Create: `tests/pipeline/test_source_processing.py`

**Interfaces:**
- Produces: `FileIngestor.ingest(path)`, `WebIngestor.ingest(url)`, `DBIngestor.ingest_sqlite(path, query)`, `APIIngestor.ingest(url)`, `DocumentParser.parse(document)`, `TextNormalizer.normalize(text)`, `DateNormalizer.normalize(value)`, `NumberNormalizer.normalize(value)`, `TextSplitter.split(document)`, and `SemanticExtractor.extract(chunks)`.

- [ ] **Step 1: Write failing tests using a temporary file, SQLite database, and local HTTP fixture**

```python
def test_all_sources_become_documents(source_fixtures):
    documents = source_fixtures.ingest_all()
    assert {doc.source_type for doc in documents} == {"file", "web", "database", "api"}
    assert all(doc.content and doc.source_id for doc in documents)

def test_processing_produces_all_semantic_outputs(source_document):
    chunks = TextSplitter(max_chars=80).split(DocumentParser().parse(source_document))
    result = SemanticExtractor().extract(chunks)
    assert result.entities
    assert result.relations
    assert result.events
    assert result.triplets
```

- [ ] **Step 2: Run the test file and verify missing-module failures**
- [ ] **Step 3: Implement safe local-capable ingestors, deterministic parsers/normalizers/splitter, and configurable rule-based extraction**
- [ ] **Step 4: Verify the source-processing tests pass without public network access**

### Task 3: Conflict detection, deduplication, and graphs

**Files:**
- Create: `auditgraph/conflicts/{__init__,detector}.py`
- Create: `auditgraph/deduplication/{__init__,resolver}.py`
- Create: `auditgraph/kg/{__init__,knowledge_graph}.py`
- Create: `auditgraph/context/{__init__,context_graph}.py`
- Create: `tests/graph/test_graph_pipeline.py`

**Interfaces:**
- Consumes: extracted `Entity`, `Relation`, and `Triplet` records.
- Produces: `ConflictDetector.detect(triplets)`, `EntityResolver.resolve(entities)`, `KnowledgeGraph`, and `ContextGraph` with node/edge queries, temporal snapshots, decision chain traversal, and `to_kg_dict()`.

- [ ] **Step 1: Write failing graph-quality tests**

```python
def test_conflicts_are_flagged_without_overwrite(conflicting_triplets):
    conflicts = ConflictDetector().detect(conflicting_triplets)
    assert conflicts[0].kind == "value"
    assert set(conflicts[0].values) == {"medium", "high"}

def test_aliases_merge_and_keep_sources(alias_entities):
    resolved = EntityResolver().resolve(alias_entities)
    assert len(resolved) == 1
    assert resolved[0].source_ids == {"crm", "core"}

def test_context_graph_traces_explicit_causal_edges():
    graph = ContextGraph()
    graph.add_node("d1", "decision")
    graph.add_node("d2", "decision")
    graph.add_causal_relationship("d1", "d2", "CAUSED")
    assert graph.trace_decision_chain("d2")[0]["source"] == "d1"
```

- [ ] **Step 2: Run the graph tests and verify missing behavior**
- [ ] **Step 3: Implement non-destructive conflict reporting, canonical-key deduplication, indexed in-memory graphs, temporal state, and causal traversal**
- [ ] **Step 4: Verify graph tests pass**

### Task 4: Ontology validation and deterministic reasoning

**Files:**
- Create: `auditgraph/ontology/{__init__,models,validator}.py`
- Create: `auditgraph/reasoning/{__init__,rule_engine}.py`
- Create: `tests/reasoning/test_rule_engine.py`

**Interfaces:**
- Produces: `Ontology`, `OntologyClass`, `PropertyConstraint`, `OntologyValidator.validate(graph, ontology)`, `Rule`, `Condition`, `RuleEngine.evaluate(facts)`, and `ReasoningResult` with matched facts and explanation steps.

- [ ] **Step 1: Write failing positive and negative rule tests**

```python
def test_rule_engine_emits_explainable_conclusion():
    engine = RuleEngine([Rule("POL-001", "1.0", [Condition("risk_score", ">=", 70)], "manual_review")])
    result = engine.evaluate({"application_id": "A-1", "risk_score": 82})
    assert result.conclusions == ["manual_review"]
    assert result.matches[0].rule_id == "POL-001"
    assert result.matches[0].facts["risk_score"] == 82

def test_rule_engine_does_not_fire_when_condition_fails():
    result = engine.evaluate({"risk_score": 20})
    assert result.conclusions == []
```

- [ ] **Step 2: Run the reasoning tests and verify failure**
- [ ] **Step 3: Implement typed ontology constraints and deterministic operators `==`, `!=`, `>`, `>=`, `<`, `<=`, `in`, and `contains`**
- [ ] **Step 4: Verify reasoning and ontology tests pass**

### Task 5: Decisions, approvals, exceptions, and provenance chain

**Files:**
- Create: `auditgraph/context/decision_recorder.py`
- Create: `auditgraph/provenance/{__init__,integrity,storage,manager}.py`
- Create: `tests/context/test_decision_audit.py`

**Interfaces:**
- Consumes: `ContextGraph`, decision-related dataclasses, rule results, and source IDs.
- Produces: `DecisionRecorder.record_decision()`, `record_approval()`, `record_policy_exception()`, `ProvenanceManager.track()`, `trace()`, `invalidate()`, and `verify_chain()`.

- [ ] **Step 1: Write failing decision and tamper-detection tests**

```python
def test_decision_links_evidence_rule_and_approval(recorder):
    decision = recorder.record_decision(..., evidence_ids=["fact:1"], rule_refs=[("POL-001", "1.0")])
    recorder.record_approval(decision.decision_id, approver="risk_manager", method="system", context="reviewed")
    assert recorder.graph.get_neighbors(decision.decision_id)

def test_hash_chain_detects_tampering(provenance):
    provenance.track("source:1", "source", {"uri": "file://policy.txt"})
    provenance.track("decision:1", "decision", {"outcome": "review"}, derived_from=["source:1"])
    assert provenance.verify_chain().valid
    provenance.storage.unsafe_update_payload("decision:1", {"outcome": "approve"})
    assert not provenance.verify_chain().valid
```

- [ ] **Step 2: Run the tests and verify failure**
- [ ] **Step 3: Implement graph-backed decision links and append-only SHA-256 records with SQLite/in-memory storage, derivation tracing, and tombstone invalidation**
- [ ] **Step 4: Verify decision and provenance tests pass**

### Task 6: End-to-end pipeline, query, export, visualization, and compliance report

**Files:**
- Create: `auditgraph/pipeline/{__init__,audit_pipeline}.py`
- Create: `auditgraph/export/{__init__,json_exporter,rdf_exporter}.py`
- Create: `auditgraph/visualization/{__init__,html_visualizer}.py`
- Create: `auditgraph/cli.py`
- Create: `examples/banking_audit/{README.md,policy.txt,application.json,run.py}`
- Create: `tests/integration/test_end_to_end.py`
- Create: `README.md`

**Interfaces:**
- Consumes: all previous stage APIs.
- Produces: `AuditPipeline.run(sources, policy, approval, output_dir) -> PipelineResult`, JSON/Turtle/HTML artifacts, and `python -m auditgraph.cli demo --output <dir>`.

- [ ] **Step 1: Write a failing end-to-end test**

```python
def test_end_to_end_pipeline_produces_auditable_decision(tmp_path, source_fixtures):
    result = AuditPipeline().run(source_fixtures.all(), output_dir=tmp_path)
    assert result.stage_counts["documents"] == 4
    assert result.stage_counts["entities"] > 0
    assert result.decision_id
    assert result.compliant is True
    assert result.audit_chain_valid is True
    assert {p.suffix for p in result.exports} == {".json", ".ttl", ".html"}
```

- [ ] **Step 2: Run the integration test and verify failure at the missing orchestrator**
- [ ] **Step 3: Implement orchestration with stage-aware errors, graph queries, compliance summary, deterministic exports, HTML graph rendering, and CLI output**
- [ ] **Step 4: Run the focused integration test and then `python -m pytest -q`**
- [ ] **Step 5: Run `python -m auditgraph.cli demo --output /tmp/auditgraph-demo` and verify JSON, Turtle, and HTML artifacts plus a valid chain report**

## Plan self-review

- Coverage: all user-approved flow stages map to Tasks 2-6; Task 1 supplies stable boundaries.
- Isolation: the project has no Git metadata, so worktree and commit steps are intentionally omitted.
- Type consistency: stage records originate in `auditgraph.core.models`; downstream modules consume those exact dataclasses.
- External dependencies: no production feature or test requires public network, external database, or remote LLM.
