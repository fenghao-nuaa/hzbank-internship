"""Strict scope and artifact-type filtering for memory retrieval."""

from collections.abc import Iterable

from dream.retrieval.config import infer_domain, normalize_domain
from dream.retrieval.models import MemoryRecord, RetrievalQuery
from dream.retrieval.ranker import relevance_tokens


class MemoryFilters:
    def apply(
        self,
        records: Iterable[MemoryRecord],
        query: RetrievalQuery,
    ) -> tuple[MemoryRecord, ...]:
        requested_kinds = set(query.kinds)
        requested_domain = normalize_domain(query.domain)
        query_tokens = relevance_tokens(query.text)
        return tuple(
            record
            for record in records
            if record.tenant_id == query.tenant_id
            and record.agent_id == query.agent_id
            and record.user_id in {None, query.user_id}
            and (not requested_kinds or record.kind in requested_kinds)
            and self._domain_relevant(
                record,
                requested_domain=requested_domain,
                query_tokens=query_tokens,
            )
        )

    @staticmethod
    def _domain_relevant(
        record: MemoryRecord,
        *,
        requested_domain: str | None,
        query_tokens: frozenset[str],
    ) -> bool:
        if requested_domain is None:
            return True
        inferred = infer_domain(record.content)
        if inferred is not None:
            return normalize_domain(inferred) == requested_domain
        content_tokens = relevance_tokens(record.content)
        return bool(query_tokens & content_tokens)
