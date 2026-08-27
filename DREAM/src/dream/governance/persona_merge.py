"""Deterministic, lossless planning for atomic user-persona updates."""

from dataclasses import dataclass
import re

from dream.governance.knowledge import CandidateKnowledge
from dream.governance.persona_models import (
    PersonaCandidate,
    PersonaCanonicalizationRequired,
    PersonaMergeType,
    parse_persona_atom,
    render_persona_atom,
)
from dream.memory.items import AtomicMemoryItem, memory_id_for, parse_memory_items


_WORD = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.IGNORECASE)
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "be",
        "for",
        "in",
        "is",
        "of",
        "on",
        "or",
        "preference",
        "preferences",
        "prefer",
        "prefers",
        "require",
        "requires",
        "that",
        "the",
        "this",
        "to",
        "user",
        "when",
        "with",
    }
)


@dataclass(frozen=True)
class PersonaMutation:
    operation: str
    content: str
    memory_id: str = ""
    old_content: str = ""

    def to_payload(self, confidence: float) -> dict[str, object]:
        payload: dict[str, object] = {
            "action": self.operation,
            "target": "user",
            "content": self.content,
            "confidence": confidence,
        }
        if self.operation == "replace":
            payload["memory_id"] = self.memory_id
            payload["old_content"] = self.old_content
        return payload


class PersonaMergeStrategy:
    """Plan sequential atomic mutations without discarding durable text."""

    def __init__(self, existing_memory: str) -> None:
        self._items = list(parse_memory_items(existing_memory))

    def plan(
        self,
        candidate: CandidateKnowledge | PersonaCandidate,
    ) -> PersonaMutation | None:
        if isinstance(candidate, CandidateKnowledge):
            candidate = PersonaCandidate.from_knowledge(candidate)
        content = candidate.statement.strip()
        normalized = self._normalized(content)
        for item in self._items:
            if (
                self._normalized(parse_persona_atom(item.content).statement)
                == normalized
                and not candidate.new_information
            ):
                return None
        if candidate.merge_type is PersonaMergeType.DUPLICATE:
            return None
        if candidate.merge_type is PersonaMergeType.CONFLICT:
            raise PersonaCanonicalizationRequired(
                "conflicting persona evidence requires an explicit resolution"
            )

        if candidate.merge_type is PersonaMergeType.NEW:
            rendered = render_persona_atom(candidate)
            self._items.append(
                AtomicMemoryItem(
                    memory_id=memory_id_for(rendered),
                    content=rendered,
                    raw_entry=rendered,
                )
            )
            return PersonaMutation(operation="add", content=rendered)

        target_index = self._target_index(candidate.target_memory_id)
        if target_index is None:
            target_index = self._related_index(content, domain=candidate.domain)
        if target_index is None:
            raise PersonaCanonicalizationRequired(
                f"{candidate.merge_type.value} persona has no same-domain target"
            )

        old_item = self._items[target_index]
        old_content = old_item.content
        old_atom = parse_persona_atom(old_content)
        if (
            "general" not in {old_atom.domain, candidate.domain}
            and old_atom.domain != candidate.domain
        ):
            raise PersonaCanonicalizationRequired(
                "persona update target belongs to a different domain"
            )
        merged_statement = self._lossless_merge(old_atom.statement, content)
        persisted_candidate = PersonaCandidate(
            id=(old_atom.persona_id or candidate.id),
            target_memory_id=candidate.target_memory_id,
            statement=merged_statement,
            new_information=candidate.new_information,
            evidence=candidate.evidence,
            source_event_ids=candidate.source_event_ids,
            confidence=max(old_atom.confidence, candidate.confidence),
            merge_type=candidate.merge_type,
            domain=candidate.domain,
        )
        merged = render_persona_atom(persisted_candidate)
        self._items[target_index] = AtomicMemoryItem(
            memory_id=memory_id_for(merged),
            content=merged,
            raw_entry=merged,
        )
        return PersonaMutation(
            operation="replace",
            memory_id=old_item.memory_id,
            old_content=old_content,
            content=merged,
        )

    def _target_index(self, memory_id: str) -> int | None:
        if not memory_id:
            return None
        return next(
            (index for index, item in enumerate(self._items) if item.memory_id == memory_id),
            None,
        )

    def _related_index(self, content: str, *, domain: str) -> int | None:
        candidate_tokens = self._tokens(content)
        ranked: list[tuple[int, float, int]] = []
        for index, item in enumerate(self._items):
            atom = parse_persona_atom(item.content)
            if atom.domain != domain:
                continue
            item_tokens = self._tokens(atom.statement)
            overlap = len(candidate_tokens & item_tokens)
            denominator = min(len(candidate_tokens), len(item_tokens))
            score = overlap / denominator if denominator else 0.0
            ranked.append((overlap, score, -index))
        if not ranked:
            return None
        overlap, score, negative_index = max(ranked)
        if overlap < 2 or score < 0.08:
            return None
        return -negative_index

    @classmethod
    def _tokens(cls, content: str) -> set[str]:
        tokens: set[str] = set()
        for raw in _WORD.findall(content.casefold()):
            if raw.isascii():
                if len(raw) > 1 and raw not in _STOP_WORDS:
                    tokens.add(raw)
                continue
            if len(raw) == 1:
                tokens.add(raw)
            else:
                tokens.update(raw[index : index + 2] for index in range(len(raw) - 1))
        return tokens

    @staticmethod
    def _normalized(content: str) -> str:
        return " ".join(content.split()).casefold()

    @classmethod
    def _lossless_merge(cls, old_content: str, new_content: str) -> str:
        if cls._normalized(old_content) in cls._normalized(new_content):
            return new_content.strip()
        if cls._normalized(new_content) in cls._normalized(old_content):
            return old_content.strip()
        return f"{old_content.rstrip()}\n{new_content.strip()}"
