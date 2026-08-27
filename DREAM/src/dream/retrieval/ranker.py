"""Deterministic local relevance ranking without model dependencies."""

from collections.abc import Iterable
from datetime import datetime, timezone
import re

from dream.retrieval.config import normalize_domain
from dream.retrieval.models import MemoryKind, MemoryRecord, RankedMemory


_TOKEN = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]")
_KIND_WEIGHT = {
    MemoryKind.DECISION_RULE: 0.30,
    MemoryKind.DECISION_CARD: 0.25,
    MemoryKind.USER_PERSONA: 0.20,
    MemoryKind.SKILL_CANDIDATE: 0.10,
}


def relevance_tokens(text: str) -> frozenset[str]:
    return frozenset(match.group(0).casefold() for match in _TOKEN.finditer(text))


class LexicalRanker:
    def rank(
        self,
        records: Iterable[MemoryRecord],
        query_text: str,
        *,
        query_domain: str | None = None,
    ) -> tuple[RankedMemory, ...]:
        query_tokens = relevance_tokens(query_text)
        ranked = tuple(
            RankedMemory(
                record=record,
                score=self._score(
                    record,
                    query_tokens,
                    query_domain=query_domain,
                ),
            )
            for record in records
        )
        return tuple(
            sorted(
                ranked,
                key=lambda item: (
                    -item.score,
                    item.record.memory_id,
                ),
            )
        )

    @staticmethod
    def _score(
        record: MemoryRecord,
        query_tokens: frozenset[str],
        *,
        query_domain: str | None,
    ) -> float:
        if not query_tokens:
            relevance = 0.0
        else:
            relevance = len(relevance_tokens(record.content) & query_tokens) / len(
                query_tokens
            )
        domain_bonus = (
            0.15
            if query_domain is not None
            and normalize_domain(record.domain) == normalize_domain(query_domain)
            else 0.0
        )
        return (
            relevance * 10
            + _KIND_WEIGHT[record.kind]
            + record.confidence * 0.1
            + domain_bonus
            + LexicalRanker._recency(record.updated_at)
        )

    @staticmethod
    def _recency(updated_at: str) -> float:
        if not updated_at:
            return 0.0
        try:
            value = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        age_days = max(
            0.0,
            (
                datetime.now(timezone.utc) - value.astimezone(timezone.utc)
            ).total_seconds()
            / 86_400,
        )
        return max(0.0, 0.05 * (1 - min(age_days, 365) / 365))
