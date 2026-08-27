"""Canonical knowledge proposals discovered by an external review model."""

from dataclasses import dataclass, field
from enum import StrEnum


class KnowledgeType(StrEnum):
    USER_PREFERENCE = "user_preference"
    DECISION_RULE = "decision_rule"
    WORKFLOW_SKILL = "workflow_skill"


@dataclass(frozen=True)
class CandidateKnowledge:
    knowledge_type: KnowledgeType
    content: str
    confidence: float
    source_event_ids: tuple[str, ...]
    knowledge_id: str = ""
    attributes: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "type": self.knowledge_type.value,
            "id": self.knowledge_id,
            "content": self.content,
            "confidence": self.confidence,
            "source_event_ids": list(self.source_event_ids),
            **self.attributes,
        }
        if self.knowledge_type is KnowledgeType.WORKFLOW_SKILL:
            value["status"] = "pending_skill_implementation"
        return value


@dataclass(frozen=True)
class KnowledgeProposal:
    candidates: tuple[CandidateKnowledge, ...]

    def to_dict(self) -> dict[str, object]:
        return {"knowledge_candidates": [value.to_dict() for value in self.candidates]}
