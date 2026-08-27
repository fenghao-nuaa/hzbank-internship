"""Provider-independent models for runtime memory retrieval."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping


class MemoryKind(StrEnum):
    USER_PERSONA = "user_persona"
    DECISION_RULE = "decision_rule"
    DECISION_CARD = "decision_card"
    SKILL_CANDIDATE = "skill_candidate"


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    kind: MemoryKind
    content: str
    tenant_id: str
    agent_id: str
    user_id: str | None = None
    confidence: float = 1.0
    source_event_ids: tuple[str, ...] = ()
    source: str = ""
    domain: str | None = None
    updated_at: str = ""
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for label, value in (
            ("memory_id", self.memory_id),
            ("tenant_id", self.tenant_id),
            ("agent_id", self.agent_id),
            ("content", self.content),
        ):
            if not value.strip():
                raise ValueError(f"{label} must be non-empty")
        if isinstance(self.confidence, bool) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between zero and one")
        if self.domain is not None and not self.domain.strip():
            raise ValueError("domain must be non-empty when provided")
        if self.updated_at:
            try:
                datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("updated_at must be ISO 8601") from exc


MemoryItem = MemoryRecord


@dataclass(frozen=True)
class MemoryRetrievalRequest:
    user_id: str
    query: str
    task_context: Mapping[str, object] = field(default_factory=dict)
    limit: int = 5

    def __post_init__(self) -> None:
        if not self.user_id.strip():
            raise ValueError("user_id must be non-empty")
        if not self.query.strip():
            raise ValueError("query must be non-empty")
        if self.limit < 1:
            raise ValueError("retrieval limit must be positive")


@dataclass(frozen=True)
class RetrievalQuery:
    text: str
    tenant_id: str
    agent_id: str
    user_id: str
    kinds: tuple[MemoryKind, ...] = ()
    limit: int = 8
    domain: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("tenant_id must be non-empty")
        if not self.agent_id.strip():
            raise ValueError("agent_id must be non-empty")
        if not self.user_id.strip():
            raise ValueError("user_id must be non-empty")
        if self.limit < 1:
            raise ValueError("retrieval limit must be positive")
        if self.domain is not None and not self.domain.strip():
            raise ValueError("domain must be non-empty when provided")


@dataclass(frozen=True)
class RankedMemory:
    record: MemoryRecord
    score: float


@dataclass(frozen=True)
class RetrievalResult:
    query: RetrievalQuery
    matches: tuple[RankedMemory, ...]


@dataclass(frozen=True)
class RetrievedContext:
    markdown: str
    included_memory_ids: tuple[str, ...]
    estimated_tokens: int


@dataclass(frozen=True)
class RetrievedMemory:
    memory_id: str
    type: str
    content: str
    source: str
    confidence: float
    domain: str | None
    score: float

    @classmethod
    def from_ranked(cls, value: RankedMemory) -> "RetrievedMemory":
        kind = value.record.kind
        public_type = "persona" if kind is MemoryKind.USER_PERSONA else kind.value
        return cls(
            memory_id=value.record.memory_id,
            type=public_type,
            content=value.record.content,
            source=value.record.source,
            confidence=value.record.confidence,
            domain=value.record.domain,
            score=value.score,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.memory_id,
            "type": self.type,
            "content": self.content,
            "source": self.source,
            "confidence": self.confidence,
            "domain": self.domain,
            "score": self.score,
        }


@dataclass(frozen=True)
class MemoryRetrievalResponse:
    query: str
    memories: tuple[RetrievedMemory, ...]
    context: str
    domain: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "domain": self.domain,
            "memories": [memory.to_dict() for memory in self.memories],
            "context": self.context,
        }
