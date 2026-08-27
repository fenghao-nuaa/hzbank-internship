"""Build a bounded Markdown context from ranked memory records."""

from datetime import datetime, timezone
import re

from dream.retrieval.models import RankedMemory, RetrievedContext, RetrievalResult


_TOKEN = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]")


def estimated_tokens(text: str) -> int:
    return len(tuple(_TOKEN.finditer(text)))


class ContextBuilder:
    def __init__(
        self,
        token_budget: int = 4_000,
        *,
        duplicate_similarity: float = 0.8,
    ) -> None:
        if token_budget < 1:
            raise ValueError("context token budget must be positive")
        if not 0 <= duplicate_similarity <= 1:
            raise ValueError("duplicate_similarity must be between zero and one")
        self.token_budget = token_budget
        self.duplicate_similarity = duplicate_similarity

    def build(self, result: RetrievalResult) -> RetrievedContext:
        chunks: list[str] = []
        memory_ids: list[str] = []
        remaining = self.token_budget
        for match in self._curate(result.matches)[: result.query.limit]:
            record = match.record
            chunk = (
                f"[{record.kind.value}:{record.memory_id}]\n{record.content.strip()}\n"
            )
            token_count = estimated_tokens(chunk)
            if token_count <= remaining:
                chunks.append(chunk)
                memory_ids.append(record.memory_id)
                remaining -= token_count
                continue
            if chunks:
                continue
            excerpt = self._truncate(chunk, remaining)
            if excerpt:
                chunks.append(excerpt)
                memory_ids.append(record.memory_id)
                remaining = 0
            break
        markdown = "\n".join(chunks)
        return RetrievedContext(
            markdown=markdown,
            included_memory_ids=tuple(memory_ids),
            estimated_tokens=estimated_tokens(markdown),
        )

    def _curate(
        self,
        matches: tuple[RankedMemory, ...],
    ) -> tuple[RankedMemory, ...]:
        conflict_winners: dict[str, RankedMemory] = {}
        for match in matches:
            key = match.record.metadata.get("conflict_key")
            if not isinstance(key, str) or not key.strip():
                continue
            current = conflict_winners.get(key)
            if current is None or self._preference(match) > self._preference(current):
                conflict_winners[key] = match

        selected: list[RankedMemory] = []
        selected_tokens: list[frozenset[str]] = []
        for match in matches:
            key = match.record.metadata.get("conflict_key")
            if (
                isinstance(key, str)
                and key.strip()
                and conflict_winners[key] is not match
            ):
                continue
            tokens = self._semantic_tokens(match.record.content)
            if any(
                self._similarity(tokens, existing) >= self.duplicate_similarity
                for existing in selected_tokens
            ):
                continue
            selected.append(match)
            selected_tokens.append(tokens)
        return tuple(selected)

    @staticmethod
    def _preference(match: RankedMemory) -> tuple[float, float, float]:
        timestamp = 0.0
        if match.record.updated_at:
            try:
                value = datetime.fromisoformat(
                    match.record.updated_at.replace("Z", "+00:00")
                )
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                timestamp = value.timestamp()
            except ValueError:
                timestamp = 0.0
        return timestamp, match.record.confidence, match.score

    @staticmethod
    def _semantic_tokens(text: str) -> frozenset[str]:
        values: set[str] = set()
        for match in _TOKEN.finditer(text):
            value = match.group(0).casefold()
            if value.isascii() and value.endswith("s") and len(value) > 3:
                value = value[:-1]
            values.add(value)
        return frozenset(values)

    @staticmethod
    def _similarity(left: frozenset[str], right: frozenset[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / len(left | right)

    @staticmethod
    def _truncate(text: str, budget: int) -> str:
        matches = tuple(_TOKEN.finditer(text))
        if not matches or budget < 1:
            return ""
        if len(matches) <= budget:
            return text
        return text[: matches[budget - 1].end()].rstrip() + "\n"
