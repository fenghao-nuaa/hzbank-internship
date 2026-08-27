from auditgraph.context import ContextGraph
from auditgraph.ontology import Ontology, OntologyClass, OntologyValidator, PropertyConstraint
from auditgraph.reasoning import Condition, Rule, RuleEngine


def test_rule_engine_emits_explainable_conclusion() -> None:
    engine = RuleEngine(
        [
            Rule(
                rule_id="POL-001",
                version="1.0",
                conditions=[Condition("risk_score", ">=", 70)],
                conclusion="manual_review",
                source_ref="policy.txt#article-3",
            )
        ]
    )
    result = engine.evaluate({"application_id": "A-1", "risk_score": 82})
    assert result.conclusions == ["manual_review"]
    assert result.matches[0].rule_id == "POL-001"
    assert result.matches[0].facts["risk_score"] == 82
    assert "risk_score >= 70" in result.matches[0].steps[0]


def test_rule_engine_does_not_fire_when_condition_fails() -> None:
    engine = RuleEngine(
        [Rule("POL-001", "1.0", [Condition("risk_score", ">=", 70)], "manual_review")]
    )
    result = engine.evaluate({"risk_score": 20})
    assert result.conclusions == []
    assert result.matches == []


def test_rule_engine_supports_membership_and_contains() -> None:
    engine = RuleEngine(
        [
            Rule(
                "POL-002",
                "1.0",
                [Condition("country", "in", ["IR", "KP"]), Condition("tags", "contains", "rapid")],
                "enhanced_due_diligence",
            )
        ]
    )
    result = engine.evaluate({"country": "IR", "tags": ["rapid", "cross_border"]})
    assert result.conclusions == ["enhanced_due_diligence"]


def test_ontology_validator_reports_missing_required_property() -> None:
    graph = ContextGraph()
    graph.add_node("A-1", "LoanApplication", amount=100_000)
    ontology = Ontology(
        ontology_id="banking",
        version="1.0",
        classes={
            "LoanApplication": OntologyClass(
                name="LoanApplication",
                constraints=[PropertyConstraint("risk_score", required=True, value_type="number")],
            )
        },
    )
    result = OntologyValidator().validate(graph, ontology)
    assert result.valid is False
    assert result.errors[0]["property"] == "risk_score"


def test_ontology_validator_accepts_conforming_graph() -> None:
    graph = ContextGraph()
    graph.add_node("A-1", "LoanApplication", risk_score=82)
    ontology = Ontology(
        ontology_id="banking",
        version="1.0",
        classes={
            "LoanApplication": OntologyClass(
                name="LoanApplication",
                constraints=[PropertyConstraint("risk_score", required=True, value_type="number")],
            )
        },
    )
    assert OntologyValidator().validate(graph, ontology).valid is True
