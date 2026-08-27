"""Behavior contracts for the disconnected Retrieval-layer framework."""

from pathlib import Path

from dream.application.service import DreamService


def _record(
    memory_id: str,
    content: str,
    *,
    tenant_id: str = "tenant-a",
    agent_id: str = "agent-a",
    user_id: str | None = "user-a",
    kind_value: str = "user_persona",
    confidence: float = 0.9,
):
    from dream.retrieval.models import MemoryKind, MemoryRecord

    return MemoryRecord(
        memory_id=memory_id,
        kind=MemoryKind(kind_value),
        content=content,
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        confidence=confidence,
        source_event_ids=("evt-1",),
    )


def test_filters_enforce_scope_and_requested_memory_kinds() -> None:
    from dream.retrieval.filters import MemoryFilters
    from dream.retrieval.models import MemoryKind, RetrievalQuery

    records = (
        _record("persona", "concise answers"),
        _record(
            "card",
            "verify supplier accounts",
            user_id=None,
            kind_value="decision_card",
        ),
        _record("other-user", "private preference", user_id="user-b"),
        _record("other-agent", "other agent rule", agent_id="agent-b"),
    )
    query = RetrievalQuery(
        text="supplier verification",
        tenant_id="tenant-a",
        agent_id="agent-a",
        user_id="user-a",
        kinds=(MemoryKind.DECISION_CARD,),
    )

    selected = MemoryFilters().apply(records, query)

    assert tuple(record.memory_id for record in selected) == ("card",)


def test_ranker_orders_relevant_memory_deterministically() -> None:
    from dream.retrieval.ranker import LexicalRanker

    records = (
        _record("unrelated", "prefers Python examples", confidence=0.99),
        _record("relevant", "supplier account verification workflow"),
        _record("partial", "supplier onboarding checklist"),
    )

    ranked = LexicalRanker().rank(records, "supplier account verification")

    assert tuple(item.record.memory_id for item in ranked) == (
        "relevant",
        "partial",
        "unrelated",
    )
    assert ranked[0].score > ranked[1].score > ranked[2].score


def test_retriever_composes_source_filtering_ranking_and_limit() -> None:
    from dream.retrieval.models import RetrievalQuery
    from dream.retrieval.retriever import MemoryRetriever

    class Source:
        def list_records(self):
            return (
                _record("first", "payment exception verification"),
                _record("second", "payment reconciliation"),
                _record("wrong-user", "payment verification", user_id="user-b"),
            )

    result = MemoryRetriever(Source()).retrieve(
        RetrievalQuery(
            text="payment verification",
            tenant_id="tenant-a",
            agent_id="agent-a",
            user_id="user-a",
            limit=2,
        )
    )

    assert tuple(item.record.memory_id for item in result.matches) == (
        "first",
        "second",
    )


def test_context_builder_respects_budget_and_reports_included_records() -> None:
    from dream.retrieval.context_builder import ContextBuilder, estimated_tokens
    from dream.retrieval.models import RankedMemory, RetrievalQuery, RetrievalResult

    result = RetrievalResult(
        query=RetrievalQuery(
            text="payment",
            tenant_id="tenant-a",
            agent_id="agent-a",
            user_id="user-a",
        ),
        matches=(
            RankedMemory(_record("first", "verify payment status"), 1.0),
            RankedMemory(
                _record("second", "preserve approval evidence before payment"),
                0.5,
            ),
        ),
    )

    context = ContextBuilder(token_budget=12).build(result)

    assert context.included_memory_ids == ("first",)
    assert "verify payment status" in context.markdown
    assert "preserve approval evidence" not in context.markdown
    assert estimated_tokens(context.markdown) <= 12


def test_retrieval_framework_is_not_wired_into_active_service(tmp_path: Path) -> None:
    service = DreamService(tmp_path)

    assert type(service.context_selector).__module__ == "dream.retrieval.selector"
