"""Provider-facing review output adapter for DREAM's canonical protocol."""

from dataclasses import dataclass
import json
import re
from typing import Any

from dream.governance.canonicalizer import KnowledgeAdapter
from dream.governance.router import KnowledgeRouter
from dream.governance.persona_models import PersonaCanonicalizationRequired
from dream.memory.items import (
    InvalidReplaceTarget,
    parse_memory_items,
    require_atomic_text,
    resolve_replace_target,
)
from dream.extraction.models import ArtifactKind, ReviewAction, ReviewEventDisposition
from dream.extraction.structured import StructuredToolCall


_CARD_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_MAX_TEXT_LENGTH = 4_000
_SOURCE_FIELDS = (
    "source_event_ids",
    "source_events",
    "sourceEvents",
    "sources",
)
_ALLOWED_WRAPPER_NAMES = frozenset({"review_batch_result"})
_RECOGNIZED_REVIEW_FIELDS = frozenset(
    {
        "knowledge_candidates",
        "memory_actions",
        "decision_card_actions",
        "skill_actions",
        "event_dispositions",
        "nothing_to_save_reason",
    }
)
_GROUPED_KNOWLEDGE_TYPES = {
    "user_persona": "user_preference",
    "decision_rules": "decision_rule",
    "skills": "workflow_skill",
}
_MAX_WRAPPER_DEPTH = 4


class InvalidReviewOutput(ValueError):
    """The provider output cannot be converted into a safe canonical review."""


@dataclass(frozen=True)
class ReviewAdapterContext:
    """Batch-local evidence and current state required during adaptation."""

    event_ids: tuple[str, ...]
    existing_memory: str
    existing_decision_cards: tuple[str, ...]
    allowed_tools_by_event: dict[str, frozenset[str]]
    require_initial_memory: bool = False
    require_initial_decision_card: bool = False


@dataclass(frozen=True)
class CanonicalReview:
    """The only review representation accepted by DREAM core services."""

    memory_actions: tuple[ReviewAction, ...]
    decision_card_actions: tuple[ReviewAction, ...]
    event_dispositions: tuple[ReviewEventDisposition, ...]
    skill_actions: tuple[ReviewAction, ...] = ()
    nothing_to_save_reason: str | None = None
    knowledge_proposal: dict[str, object] | None = None
    adapter_diagnostics: dict[str, object] | None = None

    @property
    def actions(self) -> tuple[ReviewAction, ...]:
        return self.memory_actions + self.decision_card_actions + self.skill_actions


class ReviewAdapter:
    """Convert variable provider output into DREAM's strict internal model."""

    def adapt(
        self,
        raw_llm_output: object,
        context: ReviewAdapterContext,
    ) -> CanonicalReview:
        self._validate_context(context)
        payload, diagnostics = self._payload(raw_llm_output)
        payload, before_count, normalization_changes = (
            self._normalize_knowledge_candidates(payload)
        )
        diagnostics["normalization_changes"] = list(
            diagnostics["normalization_changes"]
        ) + normalization_changes
        knowledge_proposal: dict[str, object] | None = None
        routed_actions: tuple[ReviewAction, ...] = ()
        if "knowledge_candidates" in payload:
            try:
                proposal = KnowledgeAdapter().adapt(
                    payload,
                    event_ids=context.event_ids,
                    existing_memory=context.existing_memory,
                )
            except PersonaCanonicalizationRequired as exc:
                raise InvalidReviewOutput(
                    f"PERSONA_CANONICALIZATION_REQUIRED: {exc}"
                ) from exc
            knowledge_proposal = proposal.to_dict()
            try:
                routed_actions = KnowledgeRouter().route(
                    proposal,
                    existing_memory=context.existing_memory,
                )
            except PersonaCanonicalizationRequired as exc:
                raise InvalidReviewOutput(
                    f"PERSONA_CANONICALIZATION_REQUIRED: {exc}"
                ) from exc
            self._validate_routed_tools(routed_actions, context)
            diagnostics["persona_canonicalization"] = [
                {
                    "id": candidate.knowledge_id,
                    "target_memory_id": candidate.attributes.get(
                        "target_memory_id", ""
                    ),
                    "merge_type": candidate.attributes.get("merge_type", ""),
                    "has_new_information": bool(
                        candidate.attributes.get("new_information")
                    ),
                }
                for candidate in proposal.candidates
                if candidate.knowledge_type.value == "user_preference"
            ]
        memory_items = self._object_list(
            payload.get("memory_actions"), "memory_actions"
        )
        card_items = self._object_list(
            payload.get("decision_card_actions"),
            "decision_card_actions",
        )
        skill_items = self._object_list(payload.get("skill_actions"), "skill_actions")
        disposition_items = self._object_list(
            payload.get("event_dispositions"),
            "event_dispositions",
        )

        legacy_memory_actions = tuple(
            self._memory_action(item, context) for item in memory_items
        )
        legacy_decision_actions = tuple(
            self._decision_card_action(item, context) for item in card_items
        )
        legacy_skill_actions = tuple(
            self._skill_action(item, context) for item in skill_items
        )
        memory_actions = (
            tuple(
                action
                for action in routed_actions
                if action.kind is ArtifactKind.USER_PROFILE
            )
            + legacy_memory_actions
        )
        decision_actions = (
            tuple(
                action
                for action in routed_actions
                if action.kind is ArtifactKind.DECISION_CARD
            )
            + legacy_decision_actions
        )
        skill_actions = (
            tuple(
                action for action in routed_actions if action.kind is ArtifactKind.SKILL
            )
            + legacy_skill_actions
        )
        actions = memory_actions + decision_actions + skill_actions
        proposal_sources = (
            {
                source
                for candidate in proposal.candidates
                for source in candidate.source_event_ids
            }
            if knowledge_proposal is not None
            else set()
        )
        after_candidates = (
            list(knowledge_proposal.get("knowledge_candidates", []))
            if knowledge_proposal is not None
            else []
        )
        type_counts = {
            "persona": sum(
                candidate.get("type") == "user_preference"
                for candidate in after_candidates
            ),
            "decision": sum(
                candidate.get("type") == "decision_rule"
                for candidate in after_candidates
            ),
            "skill": sum(
                candidate.get("type") == "workflow_skill"
                for candidate in after_candidates
            ),
        }
        diagnostics["candidate_counts"] = {
            "before": before_count,
            "after": len(after_candidates),
            **type_counts,
        }
        diagnostics["routed_action_counts"] = {
            "persona": len(memory_actions),
            "decision": len(decision_actions),
            "skill": len(skill_actions),
        }
        nothing_reason = self._nothing_reason(
            payload.get("nothing_to_save_reason"),
            has_durable_knowledge=bool(actions or after_candidates),
            require_reason=knowledge_proposal is not None,
        )
        dispositions = self._event_dispositions(
            disposition_items,
            actions=actions,
            proposal_sources=proposal_sources,
            context=context,
        )
        if knowledge_proposal is None:
            self._validate_bootstrap(
                context,
                memory_actions=memory_actions,
                decision_actions=decision_actions,
                nothing_reason=nothing_reason,
            )
        return CanonicalReview(
            memory_actions=memory_actions,
            decision_card_actions=decision_actions,
            skill_actions=skill_actions,
            event_dispositions=dispositions,
            nothing_to_save_reason=nothing_reason,
            knowledge_proposal=knowledge_proposal,
            adapter_diagnostics=diagnostics,
        )

    @staticmethod
    def _validate_routed_tools(
        actions: tuple[ReviewAction, ...],
        context: ReviewAdapterContext,
    ) -> None:
        for action in actions:
            for source in action.evidence_event_ids:
                if action.tool_name not in context.allowed_tools_by_event[source]:
                    raise InvalidReviewOutput(
                        "A knowledge candidate routes to a disallowed management tool."
                    )

    @staticmethod
    def _validate_context(context: ReviewAdapterContext) -> None:
        if not context.event_ids or len(set(context.event_ids)) != len(
            context.event_ids
        ):
            raise InvalidReviewOutput("Batch event IDs must be non-empty and unique.")
        if set(context.event_ids) != set(context.allowed_tools_by_event):
            raise InvalidReviewOutput(
                "Allowed tools must be defined for every batch event."
            )

    def _payload(
        self, raw: object
    ) -> tuple[dict[str, object], dict[str, object]]:
        value = self._decode(raw)
        shape: list[str] = []
        changes: list[str] = []
        wrapper_name: str | None = None
        has_arguments = False
        has_parameters = False
        if isinstance(value, StructuredToolCall):
            wrapper_name = self._wrapper_name(value.name)
            shape.append("tool_call")
            has_arguments = True
            changes.append("unwrapped structured tool call arguments")
            value = value.arguments
        elif isinstance(value, (tuple, list)):
            if len(value) != 1 or not isinstance(value[0], StructuredToolCall):
                raise InvalidReviewOutput(
                    "Exactly one review_batch_result is required."
                )
            call = value[0]
            wrapper_name = self._wrapper_name(call.name)
            shape.append("tool_call")
            has_arguments = True
            changes.append("unwrapped structured tool call arguments")
            value = call.arguments

        for _ in range(_MAX_WRAPPER_DEPTH):
            if not isinstance(value, dict):
                raise InvalidReviewOutput("Review output must be a JSON object.")
            current = dict(value)
            nested_name = current.get("name")
            if isinstance(nested_name, str) and nested_name.strip():
                wrapper_name = self._wrapper_name(nested_name)
                if "name" not in shape:
                    shape.append("name")
            if "arguments" in current:
                has_arguments = True
                if "arguments" not in shape:
                    shape.append("arguments")
                value = self._wrapper_value(current["arguments"], "arguments", changes)
                changes.append("unwrapped arguments")
                continue
            if "parameters" in current:
                has_parameters = True
                if "parameters" not in shape:
                    shape.append("parameters")
                value = self._wrapper_value(
                    current["parameters"], "parameters", changes
                )
                changes.append("unwrapped parameters")
                continue
            nested = current.get("review_batch_result")
            if isinstance(nested, (dict, str)):
                wrapper_name = "review_batch_result"
                value = self._wrapper_value(
                    nested, "review_batch_result", changes
                )
                changes.append("unwrapped review_batch_result")
                continue
            value = current
            break
        else:
            raise InvalidReviewOutput("provider payload wrapper nesting is too deep")

        if not isinstance(value, dict):
            raise InvalidReviewOutput("Review output must be a JSON object.")
        payload = dict(value)
        recognized = sorted(_RECOGNIZED_REVIEW_FIELDS.intersection(payload))
        if payload and not recognized:
            raise InvalidReviewOutput(
                "non-empty provider payload contained no recognized review fields"
            )
        return payload, {
            "provider_shape": "+".join(shape) if shape else "direct_object",
            "wrapper_name": wrapper_name,
            "has_arguments": has_arguments,
            "has_parameters": has_parameters,
            "recognized_fields": recognized,
            "normalization_changes": changes,
        }

    @staticmethod
    def _wrapper_name(value: object) -> str:
        name = str(value).strip()
        if name not in _ALLOWED_WRAPPER_NAMES:
            raise InvalidReviewOutput("unexpected review wrapper name")
        return name

    def _wrapper_value(
        self,
        value: object,
        field: str,
        changes: list[str],
    ) -> dict[str, object]:
        if isinstance(value, str):
            try:
                decoded = self._decode(value)
            except InvalidReviewOutput as exc:
                raise InvalidReviewOutput(
                    f"{field} wrapper contains invalid JSON"
                ) from exc
            changes.append(f"decoded {field} JSON")
            value = decoded
        if not isinstance(value, dict):
            raise InvalidReviewOutput(f"{field} wrapper must contain an object")
        return dict(value)

    @staticmethod
    def _normalize_knowledge_candidates(
        payload: dict[str, object],
    ) -> tuple[dict[str, object], int, list[str]]:
        if "knowledge_candidates" not in payload:
            return payload, 0, []
        raw = payload.get("knowledge_candidates")
        if not isinstance(raw, dict) or not any(
            field in raw for field in _GROUPED_KNOWLEDGE_TYPES
        ):
            if raw is None:
                return payload, 0, []
            if isinstance(raw, list):
                return payload, len(raw), []
            if isinstance(raw, dict):
                normalized = dict(payload)
                normalized["knowledge_candidates"] = [dict(raw)]
                return normalized, 1, ["wrapped single knowledge candidate"]
            raise InvalidReviewOutput(
                "knowledge_candidates must be a grouped object or candidate list"
            )

        missing = [field for field in _GROUPED_KNOWLEDGE_TYPES if field not in raw]
        if missing:
            raise InvalidReviewOutput(
                "grouped knowledge_candidates must explicitly include all categories"
            )
        flattened: list[dict[str, object]] = []
        for field, expected_type in _GROUPED_KNOWLEDGE_TYPES.items():
            values = raw[field]
            if not isinstance(values, list) or not all(
                isinstance(item, dict) for item in values
            ):
                raise InvalidReviewOutput(f"{field} must be an object list")
            for item in values:
                candidate = dict(item)
                declared_type = candidate.pop(
                    "knowledge_type", candidate.get("type", expected_type)
                )
                if declared_type != expected_type:
                    raise InvalidReviewOutput(
                        f"{field} contains a mismatched knowledge type"
                    )
                candidate["type"] = expected_type
                flattened.append(candidate)
        normalized = dict(payload)
        normalized["knowledge_candidates"] = flattened
        return normalized, len(flattened), ["flattened grouped knowledge candidates"]

    @staticmethod
    def _decode(raw: object) -> object:
        if not isinstance(raw, str):
            return raw
        text = raw.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as original_error:
            fenced = re.findall(
                r"```(?:json)?\s*(.*?)\s*```",
                text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if len(fenced) == 1:
                try:
                    return json.loads(fenced[0].strip())
                except json.JSONDecodeError as exc:
                    raise InvalidReviewOutput(
                        "Review JSON could not be decoded."
                    ) from exc
            raise InvalidReviewOutput(
                "Review JSON could not be decoded."
            ) from original_error

    @staticmethod
    def _object_list(value: object, field: str) -> tuple[dict[str, object], ...]:
        if value is None:
            return ()
        if isinstance(value, dict):
            return (dict(value),)
        if not isinstance(value, list) or not all(
            isinstance(item, dict) for item in value
        ):
            raise InvalidReviewOutput(f"{field} must be an object or object list.")
        return tuple(dict(item) for item in value)

    def _sources(
        self,
        item: dict[str, object],
        *,
        tool_name: str,
        context: ReviewAdapterContext,
    ) -> tuple[str, ...]:
        raw: object = None
        for field in _SOURCE_FIELDS:
            if field in item:
                raw = item[field]
                break
        if raw is None or raw == [] or raw == "":
            values = context.event_ids
        elif isinstance(raw, str):
            values = (raw,)
        elif isinstance(raw, (list, tuple)) and all(
            isinstance(value, str) for value in raw
        ):
            values = tuple(raw)
        else:
            raise InvalidReviewOutput("source_event_ids must be text or a string list.")
        sources = tuple(
            dict.fromkeys(value.strip() for value in values if value.strip())
        )
        if not sources:
            raise InvalidReviewOutput("source_event_ids cannot be empty.")
        if any(source not in context.allowed_tools_by_event for source in sources):
            raise InvalidReviewOutput(
                "source_event_ids contains an event outside this batch."
            )
        if any(
            tool_name not in context.allowed_tools_by_event[source]
            for source in sources
        ):
            raise InvalidReviewOutput(
                "A source event does not allow this management tool."
            )
        return sources

    def _memory_action(
        self,
        item: dict[str, object],
        context: ReviewAdapterContext,
    ) -> ReviewAction:
        operation = item.get("operation", item.get("action"))
        if operation not in {"add", "replace", "remove"}:
            raise InvalidReviewOutput("Invalid memory action.")
        content = self._text(item.get("content"), "content")
        try:
            require_atomic_text(content, field="content")
        except InvalidReplaceTarget as exc:
            raise InvalidReviewOutput(str(exc)) from exc
        payload: dict[str, object] = {
            "action": operation,
            "content": content,
            "target": "user",
        }
        if "confidence" in item:
            payload["confidence"] = self._confidence(item.get("confidence"))
        if operation in {"replace", "remove"}:
            old_content = self._text(item.get("old_content"), "old_content")
            try:
                require_atomic_text(old_content, field="old_content")
            except InvalidReplaceTarget as exc:
                raise InvalidReviewOutput(str(exc)) from exc
            payload["old_content"] = old_content
        if operation == "replace":
            memory_id = item.get("memory_id")
            if memory_id is None or not str(memory_id).strip():
                matches = [
                    memory
                    for memory in parse_memory_items(context.existing_memory)
                    if memory.content == payload["old_content"]
                ]
                if len(matches) != 1:
                    raise InvalidReviewOutput(
                        "INVALID_REPLACE_TARGET: old_content must identify one current memory item"
                    )
                memory_id = matches[0].memory_id
            normalized_id = self._text(memory_id, "memory_id")
            try:
                resolve_replace_target(
                    context.existing_memory,
                    memory_id=normalized_id,
                    old_content=str(payload["old_content"]),
                )
            except InvalidReplaceTarget as exc:
                raise InvalidReviewOutput(str(exc)) from exc
            payload["memory_id"] = normalized_id
        sources = self._sources(item, tool_name="memory_manage", context=context)
        return ReviewAction(
            kind=ArtifactKind.USER_PROFILE,
            tool_name="memory_manage",
            payload=payload,
            source_event_id=sources[0],
            source_event_ids=sources,
        )

    def _decision_card_action(
        self,
        item: dict[str, object],
        context: ReviewAdapterContext,
    ) -> ReviewAction:
        card_id = self._text(item.get("card_id", item.get("id")), "card_id")
        if not _CARD_ID.fullmatch(card_id):
            raise InvalidReviewOutput("Invalid decision card ID.")
        payload: dict[str, object] = {"id": card_id}
        for field in ("title", "scenario", "principle", "outcome", "boundaries"):
            payload[field] = self._text(item.get(field), field)
        signals = item.get("signals")
        if isinstance(signals, str):
            signals = [signals]
        if (
            not isinstance(signals, list)
            or not signals
            or not all(isinstance(signal, str) and signal.strip() for signal in signals)
        ):
            raise InvalidReviewOutput("signals must be a non-empty string list.")
        normalized_signals = [signal.strip() for signal in signals]
        if any(len(signal) > _MAX_TEXT_LENGTH for signal in normalized_signals):
            raise InvalidReviewOutput(
                "A decision signal exceeds the local length limit."
            )
        payload["signals"] = normalized_signals
        payload["confidence"] = self._confidence(item.get("confidence"))
        sources = self._sources(
            item,
            tool_name="decision_card_manage",
            context=context,
        )
        return ReviewAction(
            kind=ArtifactKind.DECISION_CARD,
            tool_name="decision_card_manage",
            payload=payload,
            source_event_id=sources[0],
            source_event_ids=sources,
        )

    def _event_dispositions(
        self,
        items: tuple[dict[str, object], ...],
        *,
        actions: tuple[ReviewAction, ...],
        proposal_sources: set[str],
        context: ReviewAdapterContext,
    ) -> tuple[ReviewEventDisposition, ...]:
        used_sources = {
            source for action in actions for source in action.evidence_event_ids
        } | proposal_sources
        if not items:
            return tuple(
                ReviewEventDisposition(
                    event_id=event_id,
                    disposition=(
                        "used" if event_id in used_sources else "no_durable_signal"
                    ),
                    reason=(
                        None
                        if event_id in used_sources
                        else "No durable action cited this event."
                    ),
                )
                for event_id in context.event_ids
            )
        normalized: list[ReviewEventDisposition] = []
        seen: set[str] = set()
        for item in items:
            event_id = self._text(item.get("event_id"), "event_id")
            if event_id in seen or event_id not in context.allowed_tools_by_event:
                raise InvalidReviewOutput(
                    "Every batch event requires exactly one disposition."
                )
            seen.add(event_id)
            raw_status = item.get("disposition")
            status = (
                raw_status.strip().casefold() if isinstance(raw_status, str) else ""
            )
            reason = item.get("reason")
            if status == "used":
                if event_id not in used_sources:
                    raise InvalidReviewOutput(
                        "A used disposition requires action evidence."
                    )
                normalized_reason = None
            elif status in {"ignored", "discard", "discarded", "no_durable_signal"}:
                if event_id in used_sources:
                    raise InvalidReviewOutput(
                        "An action source cannot be marked no_durable_signal."
                    )
                if status == "ignored" and (
                    not isinstance(reason, str) or not reason.strip()
                ):
                    reason = "Ignored by the review provider."
                normalized_reason = self._text(reason, "reason")
                status = "no_durable_signal"
            else:
                raise InvalidReviewOutput("Invalid event disposition.")
            normalized.append(
                ReviewEventDisposition(
                    event_id=event_id,
                    disposition=status,
                    reason=normalized_reason,
                )
            )
        if seen != set(context.event_ids):
            raise InvalidReviewOutput(
                "Every batch event requires exactly one disposition."
            )
        return tuple(normalized)

    def _skill_action(
        self,
        item: dict[str, object],
        context: ReviewAdapterContext,
    ) -> ReviewAction:
        skill_id = self._text(item.get("skill_id", item.get("id")), "skill_id")
        if not _CARD_ID.fullmatch(skill_id):
            raise InvalidReviewOutput("Invalid skill ID.")
        payload: dict[str, object] = {"id": skill_id}
        for field in ("title", "scenario", "output_template", "cautions"):
            payload[field] = self._text(item.get(field), field)
        for field in ("inputs", "steps"):
            value = item.get(field)
            if isinstance(value, str):
                value = [value]
            if (
                not isinstance(value, list)
                or not value
                or not all(isinstance(entry, str) and entry.strip() for entry in value)
            ):
                raise InvalidReviewOutput(f"{field} must be a non-empty string list.")
            payload[field] = [str(entry).strip() for entry in value]
        payload["confidence"] = self._confidence(item.get("confidence"))
        sources = self._sources(item, tool_name="skill_manage", context=context)
        return ReviewAction(
            kind=ArtifactKind.SKILL,
            tool_name="skill_manage",
            payload=payload,
            source_event_id=sources[0],
            source_event_ids=sources,
        )

    @staticmethod
    def _confidence(value: object) -> float:
        confidence = value
        if isinstance(confidence, str):
            try:
                confidence = float(confidence)
            except ValueError as exc:
                raise InvalidReviewOutput("confidence must be numeric.") from exc
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise InvalidReviewOutput("confidence must be numeric.")
        if not 0 <= float(confidence) <= 1:
            raise InvalidReviewOutput("confidence must be between zero and one.")
        return float(confidence)

    @staticmethod
    def _nothing_reason(
        value: object,
        *,
        has_durable_knowledge: bool,
        require_reason: bool = False,
    ) -> str | None:
        if has_durable_knowledge:
            return None
        if value is None:
            if require_reason:
                raise InvalidReviewOutput(
                    "nothing_to_save_reason is required when no knowledge candidate exists."
                )
            return None
        if not isinstance(value, str) or not value.strip():
            raise InvalidReviewOutput("nothing_to_save_reason must be non-empty text.")
        if len(value) > _MAX_TEXT_LENGTH:
            raise InvalidReviewOutput(
                "nothing_to_save_reason exceeds the local length limit."
            )
        return value.strip()

    @staticmethod
    def _validate_bootstrap(
        context: ReviewAdapterContext,
        *,
        memory_actions: tuple[ReviewAction, ...],
        decision_actions: tuple[ReviewAction, ...],
        nothing_reason: str | None,
    ) -> None:
        if nothing_reason is not None and (
            context.require_initial_memory or context.require_initial_decision_card
        ):
            raise InvalidReviewOutput(
                "An incomplete initial candidate cannot publish nothing_to_save."
            )
        if context.require_initial_memory and not memory_actions:
            raise InvalidReviewOutput(
                "A complete initial candidate requires an initial user profile."
            )
        if context.require_initial_decision_card and not decision_actions:
            raise InvalidReviewOutput(
                "A complete initial candidate requires an initial AI decision card."
            )

    @staticmethod
    def _text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise InvalidReviewOutput(f"{field} must be non-empty text.")
        normalized = value.strip()
        if len(normalized) > _MAX_TEXT_LENGTH:
            raise InvalidReviewOutput(f"{field} exceeds the local length limit.")
        return normalized
