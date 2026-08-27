"""Select bounded, task-relevant views without limiting durable storage."""

from dataclasses import dataclass
import re

from dream.memory.items import ENTRY_DELIMITER, parse_memory_items


_TOKEN = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]")
_SOURCE = re.compile(r"<!-- dream-sources?:\s*([^>]+) -->")


def _token_spans(text: str) -> list[re.Match[str]]:
    return list(_TOKEN.finditer(text))


def estimated_tokens(text: str) -> int:
    """Return a conservative local token estimate without a model dependency."""

    return len(_token_spans(text))


@dataclass(frozen=True)
class ContextBudget:
    user_profile_tokens: int = 600
    decision_rules_tokens: int = 1_200
    decision_cards_tokens: int = 2_400

    def __post_init__(self) -> None:
        if min(
            self.user_profile_tokens,
            self.decision_rules_tokens,
            self.decision_cards_tokens,
        ) < 1:
            raise ValueError("context token budgets must be positive")


@dataclass(frozen=True)
class ContextSelection:
    user_profile: str
    decision_rules: str
    decision_cards: tuple[str, ...]
    skills: tuple[str, ...] = ()


class ContextSelector:
    """Build an agent-facing context view from complete durable repositories."""

    def __init__(self, budget: ContextBudget | None = None) -> None:
        self.budget = budget or ContextBudget()

    def select(
        self,
        *,
        user_repository: str,
        user_projection: str,
        decision_rules_repository: str,
        character_projection: str,
        decision_cards: tuple[str, ...],
        query: str = "",
    ) -> ContextSelection:
        if query.strip():
            user_profile = self._select_profile(
                user_repository,
                query,
                self.budget.user_profile_tokens,
            )
        else:
            user_profile = self._truncate(
                self._user_projection(user_projection, user_repository),
                self.budget.user_profile_tokens,
            )
        decision_rules = self._truncate(
            character_projection or decision_rules_repository,
            self.budget.decision_rules_tokens,
        )
        selected_cards = self._select_documents(
            decision_cards,
            query,
            self.budget.decision_cards_tokens,
        )
        return ContextSelection(
            user_profile=user_profile,
            decision_rules=decision_rules,
            decision_cards=selected_cards,
            skills=(),
        )

    @staticmethod
    def _user_projection(projection: str, repository: str) -> str:
        if not projection:
            return repository
        sources: list[str] = []
        for match in _SOURCE.finditer(repository):
            for raw in match.group(1).split(","):
                source = raw.strip()
                if source and source not in sources:
                    sources.append(source)
        if not sources:
            return projection
        return (
            projection.rstrip()
            + "\n<!-- dream-sources: "
            + ", ".join(sources)
            + " -->\n"
        )

    def _select_profile(self, repository: str, query: str, budget: int) -> str:
        items = parse_memory_items(repository)
        selected = self._select_documents(
            tuple(item.raw_entry for item in items),
            query,
            budget,
        )
        if not selected:
            return ""
        return ENTRY_DELIMITER.join(value.strip() for value in selected) + "\n"

    def _select_documents(
        self,
        documents: tuple[str, ...],
        query: str,
        budget: int,
    ) -> tuple[str, ...]:
        query_tokens = self._relevance_tokens(query)
        ranked = sorted(
            enumerate(documents),
            key=lambda value: (
                self._relevance(value[1], query_tokens),
                value[0],
            ),
            reverse=True,
        )
        selected: list[str] = []
        remaining = budget
        for _, document in ranked:
            if remaining < 1:
                break
            token_count = estimated_tokens(document)
            if token_count <= remaining:
                selected.append(document)
                remaining -= token_count
            elif not selected:
                excerpt = self._truncate(document, remaining)
                if excerpt:
                    selected.append(excerpt)
                remaining = 0
        return tuple(selected)

    @classmethod
    def _relevance(cls, text: str, query_tokens: set[str]) -> float:
        if not query_tokens:
            return 0.0
        text_tokens = cls._relevance_tokens(text)
        return len(text_tokens & query_tokens) / len(query_tokens)

    @staticmethod
    def _relevance_tokens(text: str) -> set[str]:
        return {match.group(0).casefold() for match in _token_spans(text)}

    @staticmethod
    def _truncate(text: str, budget: int) -> str:
        matches = _token_spans(text)
        if len(matches) <= budget:
            return text
        if budget < 1:
            return ""
        return text[: matches[budget - 1].end()].rstrip() + "\n"
