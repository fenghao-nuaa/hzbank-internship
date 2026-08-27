"""Route provider-independent knowledge into DREAM canonical actions."""

import hashlib
import re

from dream.governance.knowledge import (
    CandidateKnowledge,
    KnowledgeProposal,
    KnowledgeType,
)
from dream.governance.persona_merge import PersonaMergeStrategy
from dream.extraction.models import ArtifactKind, ReviewAction


class KnowledgeRouter:
    def route(
        self,
        proposal: KnowledgeProposal,
        *,
        existing_memory: str = "",
    ) -> tuple[ReviewAction, ...]:
        persona_merge = PersonaMergeStrategy(existing_memory)
        actions: list[ReviewAction] = []
        for candidate in proposal.candidates:
            if candidate.knowledge_type is KnowledgeType.WORKFLOW_SKILL:
                continue
            if candidate.knowledge_type is KnowledgeType.USER_PREFERENCE:
                mutation = persona_merge.plan(candidate)
                if mutation is None:
                    continue
                action = ReviewAction(
                    kind=ArtifactKind.USER_PROFILE,
                    tool_name="memory_manage",
                    payload=mutation.to_payload(candidate.confidence),
                    source_event_id=candidate.source_event_ids[0],
                    source_event_ids=candidate.source_event_ids,
                )
            else:
                action = self._route_one(candidate)
            actions.append(action)
        return tuple(actions)

    def _route_one(self, candidate: CandidateKnowledge) -> ReviewAction:
        if candidate.knowledge_type is KnowledgeType.DECISION_RULE:
            kind = ArtifactKind.DECISION_CARD
            tool = "decision_card_manage"
            payload = self._decision_payload(candidate)
        else:
            raise ValueError("only decision rules are routed by _route_one")
        return ReviewAction(
            kind=kind,
            tool_name=tool,
            payload=payload,
            source_event_id=candidate.source_event_ids[0],
            source_event_ids=candidate.source_event_ids,
        )

    def _decision_payload(self, candidate: CandidateKnowledge) -> dict[str, object]:
        attributes = candidate.attributes
        return {
            "id": self._identifier(candidate),
            "title": str(
                attributes.get("name", attributes.get("title", candidate.content))
            ),
            "scenario": str(
                attributes.get("trigger", attributes.get("scenario", candidate.content))
            ),
            "signals": self._list(
                attributes.get("signals"),
                "Relevant situation matches the extracted rule.",
            ),
            "principle": candidate.content,
            "outcome": str(
                attributes.get("outcome", "Apply the extracted rule consistently.")
            ),
            "boundaries": self._text_or_list(
                attributes.get("constraints", attributes.get("boundaries")),
                "Use only in contexts supported by the cited events.",
            ),
            "confidence": candidate.confidence,
        }

    def _skill_payload(self, candidate: CandidateKnowledge) -> dict[str, object]:
        attributes = candidate.attributes
        return {
            "id": self._identifier(candidate),
            "title": str(
                attributes.get("name", attributes.get("title", candidate.content))
            ),
            "scenario": str(
                attributes.get("trigger", attributes.get("scenario", candidate.content))
            ),
            "inputs": self._list(attributes.get("inputs"), "Verified task context"),
            "steps": self._list(attributes.get("steps"), candidate.content),
            "output_template": str(
                attributes.get("output_template", candidate.content)
            ),
            "cautions": self._text_or_list(
                attributes.get("constraints", attributes.get("cautions")),
                "Use only verified information and preserve required approvals.",
            ),
            "confidence": candidate.confidence,
        }

    @staticmethod
    def _list(value: object, fallback: str) -> list[str]:
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        if isinstance(value, list):
            result = [
                item.strip() for item in value if isinstance(item, str) and item.strip()
            ]
            if result:
                return result
        return [fallback]

    @classmethod
    def _text_or_list(cls, value: object, fallback: str) -> str:
        return "; ".join(cls._list(value, fallback))

    @staticmethod
    def _identifier(candidate: CandidateKnowledge) -> str:
        raw = candidate.knowledge_id.strip().lower().replace("_", "-")
        slug = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
        if slug:
            return slug[:64]
        digest = hashlib.sha256(candidate.content.encode("utf-8")).hexdigest()[:12]
        return f"{candidate.knowledge_type.value.replace('_', '-')}-{digest}"
