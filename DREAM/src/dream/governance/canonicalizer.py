"""Normalize provider output into a provider-independent knowledge proposal."""

import json
import re

from dream.governance.knowledge import (
    CandidateKnowledge,
    KnowledgeProposal,
    KnowledgeType,
)
from dream.governance.persona_models import PersonaCanonicalizer
from dream.extraction.structured import StructuredToolCall


class InvalidKnowledgeProposal(ValueError):
    """Knowledge extraction output cannot be normalized safely."""


class KnowledgeAdapter:
    def adapt(
        self,
        raw: object,
        *,
        event_ids: tuple[str, ...],
        existing_memory: str = "",
    ) -> KnowledgeProposal:
        payload = self._payload(raw)
        values = payload.get("knowledge_candidates", [])
        if values is None:
            values = []
        if isinstance(values, dict):
            values = [values]
        if not isinstance(values, list) or not all(
            isinstance(value, dict) for value in values
        ):
            raise InvalidKnowledgeProposal("knowledge_candidates must be a list")
        allowed = set(event_ids)
        persona_canonicalizer = PersonaCanonicalizer(existing_memory)
        candidates: list[CandidateKnowledge] = []
        for value in values:
            try:
                knowledge_type = KnowledgeType(str(value.get("type", "")))
            except ValueError as exc:
                raise InvalidKnowledgeProposal("unknown knowledge type") from exc
            content = value.get("content")
            if not isinstance(content, str) or not content.strip():
                raise InvalidKnowledgeProposal("knowledge content must be non-empty")
            confidence = value.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise InvalidKnowledgeProposal("knowledge confidence is required")
            if not 0 <= float(confidence) <= 1:
                raise InvalidKnowledgeProposal("knowledge confidence is out of range")
            sources = value.get("source_event_ids")
            if isinstance(sources, str):
                sources = [sources]
            if not isinstance(sources, list) or not all(
                isinstance(source, str) for source in sources
            ):
                raise InvalidKnowledgeProposal("knowledge sources must be strings")
            normalized_sources = tuple(
                dict.fromkeys(source.strip() for source in sources if source.strip())
            )
            if not normalized_sources or any(
                source not in allowed for source in normalized_sources
            ):
                raise InvalidKnowledgeProposal("knowledge source is outside the batch")
            reserved = {
                "type",
                "id",
                "skill_id",
                "card_id",
                "content",
                "confidence",
                "source_event_ids",
            }
            candidate = CandidateKnowledge(
                knowledge_type=knowledge_type,
                knowledge_id=str(
                    value.get("id", value.get("skill_id", value.get("card_id", "")))
                ).strip(),
                content=content.strip(),
                confidence=float(confidence),
                source_event_ids=normalized_sources,
                attributes={
                    key: item for key, item in value.items() if key not in reserved
                },
            )
            if knowledge_type is KnowledgeType.USER_PREFERENCE:
                persona = persona_canonicalizer.canonicalize(candidate)
                candidate = CandidateKnowledge(
                    knowledge_type=knowledge_type,
                    knowledge_id=persona.id,
                    content=persona.statement,
                    confidence=persona.confidence,
                    source_event_ids=persona.source_event_ids,
                    attributes={
                        **candidate.attributes,
                        "target_memory_id": persona.target_memory_id,
                        "statement": persona.statement,
                        "new_information": persona.new_information,
                        "evidence": list(persona.evidence),
                        "merge_type": persona.merge_type.value,
                        "domain": persona.domain,
                    },
                )
            candidates.append(candidate)
        return KnowledgeProposal(tuple(candidates))

    def _payload(self, raw: object) -> dict[str, object]:
        value = raw
        if isinstance(value, str):
            text = value.strip()
            fenced = re.fullmatch(
                r"```(?:json)?\s*(.*?)\s*```",
                text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if fenced is not None:
                text = fenced.group(1)
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise InvalidKnowledgeProposal("knowledge JSON is invalid") from exc
        if isinstance(value, (tuple, list)):
            if len(value) != 1 or not isinstance(value[0], StructuredToolCall):
                raise InvalidKnowledgeProposal("one structured result is required")
            value = value[0]
        if isinstance(value, StructuredToolCall):
            if value.name != "review_batch_result":
                raise InvalidKnowledgeProposal("unexpected structured result")
            value = value.arguments
        if not isinstance(value, dict):
            raise InvalidKnowledgeProposal("knowledge proposal must be an object")
        nested = value.get("review_batch_result")
        return dict(nested) if isinstance(nested, dict) else dict(value)
