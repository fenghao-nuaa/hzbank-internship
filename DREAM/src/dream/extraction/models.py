"""Review requests, classifications, and scoped management actions."""

from dataclasses import dataclass
from enum import Enum


class ArtifactKind(str, Enum):
    DECISION_CARD = "decision_card"
    USER_PROFILE = "user_profile"
    USER_TODO = "user_todo"
    AGENT_MEMORY = "agent_memory"
    SKILL = "skill"
    WIKI_INGEST = "wiki_ingest"
    NOTHING = "nothing"


@dataclass(frozen=True)
class ReviewRequest:
    event_id: str
    transcript_text: str
    final_response: str
    allowed_tools: frozenset[str]
    current_user_profile: str = ""
    current_decision_rules: str = ""
    current_decision_cards: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewBatchEvent:
    event_id: str
    transcript_text: str
    final_response: str
    allowed_tools: frozenset[str]


@dataclass(frozen=True)
class ReviewBatchRequest:
    events: tuple[ReviewBatchEvent, ...]
    current_user_profile: str = ""
    current_decision_rules: str = ""
    current_decision_cards: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewAction:
    kind: ArtifactKind
    tool_name: str
    payload: dict[str, object]
    source_event_id: str
    source_event_ids: tuple[str, ...] = ()

    @property
    def evidence_event_ids(self) -> tuple[str, ...]:
        return self.source_event_ids or (self.source_event_id,)


@dataclass(frozen=True)
class ReviewEventDisposition:
    event_id: str
    disposition: str
    reason: str | None = None


@dataclass(frozen=True)
class ReviewResult:
    actions: tuple[ReviewAction, ...]
    summary: str
    status: str = "success"
    error: str | None = None
    event_dispositions: tuple[ReviewEventDisposition, ...] = ()
    trace: dict[str, object] | None = None
