from datetime import timezone

import pytest

from auditgraph.core.models import (
    Approval,
    Decision,
    PolicyException,
    Relation,
    SourceDocument,
    Triplet,
)


def test_source_document_requires_source_id() -> None:
    with pytest.raises(ValueError, match="source_id"):
        SourceDocument(source_id="", source_type="file", content="data")


def test_source_document_uses_utc_timestamp() -> None:
    document = SourceDocument(source_id="policy.txt", source_type="file", content="data")
    assert document.collected_at.tzinfo == timezone.utc


def test_decision_rejects_invalid_confidence() -> None:
    with pytest.raises(ValueError, match="confidence"):
        Decision(
            category="credit",
            scenario="application A-1",
            reasoning="risk rule matched",
            outcome="manual_review",
            confidence=1.1,
        )


def test_approval_rejects_unknown_method() -> None:
    with pytest.raises(ValueError, match="method"):
        Approval(decision_id="d-1", approver="risk_manager", method="phone")


def test_policy_exception_requires_approver() -> None:
    with pytest.raises(ValueError, match="approver"):
        PolicyException(
            decision_id="d-1",
            policy_id="POL-1",
            reason="manual override",
            approver="",
            justification="verified income",
        )


@pytest.mark.parametrize(
    "record",
    [
        lambda: Relation("r1", "a", "knows", "b", "source", confidence=1.2),
        lambda: Triplet("a", "score", 1, "source", confidence=-0.1),
    ],
)
def test_semantic_records_reject_invalid_confidence(record) -> None:
    with pytest.raises(ValueError, match="confidence"):
        record()
