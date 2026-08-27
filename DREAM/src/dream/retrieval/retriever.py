"""Compose a memory source, scope filters, and deterministic ranker."""

from typing import Protocol

from dream.retrieval.filters import MemoryFilters
from dream.retrieval.models import MemoryRecord, RetrievalQuery, RetrievalResult
from dream.retrieval.ranker import LexicalRanker


class MemorySource(Protocol):
    def list_records(self) -> tuple[MemoryRecord, ...]: ...


class MemoryRetriever:
    def __init__(
        self,
        source: MemorySource,
        *,
        filters: MemoryFilters | None = None,
        ranker: LexicalRanker | None = None,
    ) -> None:
        self.source = source
        self.filters = filters or MemoryFilters()
        self.ranker = ranker or LexicalRanker()

    def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        scoped = self.filters.apply(self.source.list_records(), query)
        ranked = self.ranker.rank(
            scoped,
            query.text,
            query_domain=query.domain,
        )
        return RetrievalResult(query=query, matches=ranked[: query.limit])
