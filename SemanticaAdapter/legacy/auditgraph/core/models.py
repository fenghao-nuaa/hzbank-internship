"""Stable data contracts shared by every AuditGraph pipeline stage."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}:{uuid4()}"


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(_serialize(item) for item in value)
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value


class Serializable:
    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass(slots=True)
class SourceDocument(Serializable):
    source_id: str
    source_type: str
    content: str
    content_type: str = "text/plain"
    collected_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.source_id, "source_id")
        _require_text(self.source_type, "source_type")
        if not isinstance(self.content, str):
            raise TypeError("content must be a string")
        if self.collected_at.tzinfo is None:
            self.collected_at = self.collected_at.replace(tzinfo=timezone.utc)


@dataclass(slots=True)
class Chunk(Serializable):
    chunk_id: str
    source_id: str
    content: str
    start: int
    end: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.chunk_id, "chunk_id")
        _require_text(self.source_id, "source_id")
        if self.start < 0 or self.end < self.start:
            raise ValueError("chunk offsets are invalid")


@dataclass(slots=True)
class Entity(Serializable):
    entity_id: str
    name: str
    entity_type: str
    aliases: set[str] = field(default_factory=set)
    source_ids: set[str] = field(default_factory=set)
    confidence: float = 1.0
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.entity_id, "entity_id")
        _require_text(self.name, "name")
        _require_text(self.entity_type, "entity_type")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(slots=True)
class Relation(Serializable):
    relation_id: str
    subject_id: str
    predicate: str
    object_id: str
    source_id: str
    confidence: float = 1.0
    chunk_id: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, name in (
            (self.relation_id, "relation_id"),
            (self.subject_id, "subject_id"),
            (self.predicate, "predicate"),
            (self.object_id, "object_id"),
            (self.source_id, "source_id"),
        ):
            _require_text(value, name)
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(slots=True)
class Event(Serializable):
    event_id: str
    event_type: str
    participants: list[str]
    source_id: str
    occurred_at: str | None = None
    chunk_id: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.event_id, "event_id")
        _require_text(self.event_type, "event_type")
        _require_text(self.source_id, "source_id")


@dataclass(slots=True)
class Triplet(Serializable):
    subject: str
    predicate: str
    object: Any
    source_id: str
    confidence: float = 1.0
    chunk_id: str | None = None
    recorded_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _require_text(self.subject, "subject")
        _require_text(self.predicate, "predicate")
        _require_text(self.source_id, "source_id")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(slots=True)
class ExtractionResult(Serializable):
    entities: list[Entity] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    triplets: list[Triplet] = field(default_factory=list)


@dataclass(slots=True)
class Conflict(Serializable):
    conflict_id: str
    kind: str
    subject: str
    predicate: str
    values: list[Any]
    source_ids: set[str]
    severity: str = "medium"


@dataclass(slots=True)
class Decision(Serializable):
    category: str
    scenario: str
    reasoning: str
    outcome: str
    confidence: float
    decision_maker: str = "auditgraph"
    decision_id: str = field(default_factory=lambda: new_id("decision"))
    timestamp: datetime = field(default_factory=utc_now)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, name in (
            (self.category, "category"),
            (self.scenario, "scenario"),
            (self.reasoning, "reasoning"),
            (self.outcome, "outcome"),
            (self.decision_maker, "decision_maker"),
            (self.decision_id, "decision_id"),
        ):
            _require_text(value, name)
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(slots=True)
class Approval(Serializable):
    decision_id: str
    approver: str
    method: str
    context: str = ""
    approval_id: str = field(default_factory=lambda: new_id("approval"))
    timestamp: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    ALLOWED_METHODS = {"slack_dm", "zoom_call", "email", "system"}

    def __post_init__(self) -> None:
        _require_text(self.decision_id, "decision_id")
        _require_text(self.approver, "approver")
        if self.method not in self.ALLOWED_METHODS:
            raise ValueError(f"method must be one of {sorted(self.ALLOWED_METHODS)}")


@dataclass(slots=True)
class PolicyException(Serializable):
    decision_id: str
    policy_id: str
    reason: str
    approver: str
    justification: str
    exception_id: str = field(default_factory=lambda: new_id("exception"))
    approval_timestamp: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, name in (
            (self.decision_id, "decision_id"),
            (self.policy_id, "policy_id"),
            (self.reason, "reason"),
            (self.approver, "approver"),
            (self.justification, "justification"),
        ):
            _require_text(value, name)


@dataclass(slots=True)
class PipelineResult(Serializable):
    run_id: str
    decision_id: str
    stage_counts: dict[str, int]
    compliant: bool
    audit_chain_valid: bool
    exports: list[Path] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
