"""OpenAI-compatible, single-call batch Background Review backend."""

import json
from typing import Any

from dream.extraction.models import (
    ReviewBatchEvent,
    ReviewBatchRequest,
    ReviewRequest,
    ReviewResult,
)
from dream.extraction.prompts import DREAM_COMBINED_REVIEW_PROMPT
from dream.extraction.provider_adapter import (
    InvalidReviewOutput,
    ReviewAdapter,
    ReviewAdapterContext,
)
from dream.extraction.structured import (
    StructuredCompletionClient,
    StructuredProviderError,
    StructuredToolCall,
)
from dream.memory.items import MEMORY_ID_PATTERN, parse_memory_items


_SOURCE_EVENT_IDS = {
    "type": "array",
    "items": {"type": "string"},
    "minItems": 1,
}

_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "memory_manage",
        "description": "Add, replace, or remove a durable fact in this user's isolated profile.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                "memory_id": {
                    "type": "string",
                    "pattern": MEMORY_ID_PATTERN,
                    "description": (
                        "Required for replace. Copy the ID of exactly one current "
                        "atomic memory item; never invent it."
                    ),
                },
                "content": {"type": "string"},
                "old_content": {
                    "type": "string",
                    "description": (
                        "For replace, copy the exact content of the atomic item "
                        "identified by memory_id."
                    ),
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "source_event_ids": _SOURCE_EVENT_IDS,
            },
            "required": ["action", "content", "source_event_ids"],
            "additionalProperties": False,
        },
    },
}

_DECISION_CARD_TOOL = {
    "type": "function",
    "function": {
        "name": "decision_card_manage",
        "description": "Create or update a reusable AI decision card backed by this batch.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,63}$"},
                "title": {"type": "string"},
                "scenario": {"type": "string"},
                "signals": {"type": "array", "items": {"type": "string"}},
                "principle": {"type": "string"},
                "outcome": {"type": "string"},
                "boundaries": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "source_event_ids": _SOURCE_EVENT_IDS,
            },
            "required": [
                "id",
                "title",
                "scenario",
                "signals",
                "principle",
                "outcome",
                "boundaries",
                "confidence",
                "source_event_ids",
            ],
            "additionalProperties": False,
        },
    },
}

_SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "skill_manage",
        "description": "Create or update a reusable multi-step assistant skill.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,63}$"},
                "title": {"type": "string"},
                "scenario": {"type": "string"},
                "inputs": {"type": "array", "items": {"type": "string"}},
                "steps": {"type": "array", "items": {"type": "string"}},
                "output_template": {"type": "string"},
                "cautions": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "source_event_ids": _SOURCE_EVENT_IDS,
            },
            "required": [
                "id",
                "title",
                "scenario",
                "inputs",
                "steps",
                "output_template",
                "cautions",
                "confidence",
                "source_event_ids",
            ],
            "additionalProperties": False,
        },
    },
}

_EVENT_DISPOSITION = {
    "type": "object",
    "properties": {
        "event_id": {"type": "string"},
        "disposition": {
            "type": "string",
            "enum": ["used", "no_durable_signal"],
        },
        "reason": {"type": ["string", "null"]},
    },
    "required": ["event_id", "disposition", "reason"],
    "additionalProperties": False,
}

_KNOWLEDGE_CANDIDATE = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["user_preference", "decision_rule", "workflow_skill"],
        },
        "id": {"type": "string"},
        "name": {"type": "string"},
        "content": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "source_event_ids": _SOURCE_EVENT_IDS,
        "trigger": {"type": "string"},
        "signals": {"type": "array", "items": {"type": "string"}},
        "inputs": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "outcome": {"type": "string"},
        "output_template": {"type": "string"},
    },
    "required": ["type", "content", "confidence", "source_event_ids"],
    "additionalProperties": False,
}


def _typed_knowledge_candidate(
    knowledge_type: str,
    *,
    required_fields: tuple[str, ...] = (),
) -> dict[str, object]:
    properties = dict(_KNOWLEDGE_CANDIDATE["properties"])
    properties["type"] = {"type": "string", "enum": [knowledge_type]}
    return {
        "type": "object",
        "properties": properties,
        "required": [
            "content",
            "confidence",
            "source_event_ids",
            *required_fields,
        ],
        "additionalProperties": False,
    }


def _persona_knowledge_candidate() -> dict[str, object]:
    schema = _typed_knowledge_candidate("user_preference")
    schema["properties"].update(
        {
            "target_memory_id": {"type": "string"},
            "statement": {"type": "string"},
            "new_information": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "merge_type": {
                "type": "string",
                "enum": ["duplicate", "extension", "new", "conflict"],
            },
        }
    )
    return schema


_GROUPED_KNOWLEDGE_CANDIDATES = {
    "type": "object",
    "properties": {
        "user_persona": {
            "type": "array",
            "items": _persona_knowledge_candidate(),
        },
        "decision_rules": {
            "type": "array",
            "items": _typed_knowledge_candidate(
                "decision_rule",
                required_fields=(
                    "id",
                    "name",
                    "trigger",
                    "signals",
                    "constraints",
                    "outcome",
                ),
            ),
        },
        "skills": {
            "type": "array",
            "items": _typed_knowledge_candidate(
                "workflow_skill",
                required_fields=(
                    "id",
                    "name",
                    "trigger",
                    "inputs",
                    "steps",
                    "constraints",
                    "output_template",
                ),
            ),
        },
    },
    "required": ["user_persona", "decision_rules", "skills"],
    "additionalProperties": False,
}

_COMBINED_RESULT_TOOL = {
    "type": "function",
    "function": {
        "name": "review_batch_result",
        "description": (
            "Return durable knowledge candidates discovered in this completed batch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "knowledge_candidates": {
                    **_GROUPED_KNOWLEDGE_CANDIDATES,
                },
                "event_dispositions": {
                    "type": "array",
                    "items": _EVENT_DISPOSITION,
                },
                "nothing_to_save_reason": {
                    "type": ["string", "null"],
                    "description": (
                        "A non-empty reason only when no durable knowledge candidate "
                        "exists; otherwise null."
                    ),
                },
            },
            "required": [
                "knowledge_candidates",
                "event_dispositions",
                "nothing_to_save_reason",
            ],
            "additionalProperties": False,
        },
    },
}


class OpenAIReviewBackend:
    prompt_version = "knowledge-extraction-v6-persona-canonicalization"
    validated_semantic_cache = True

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        max_completion_tokens: int = 2000,
        structured_mode: str = "auto",
    ) -> None:
        self.client = client
        self.model = model
        self.max_completion_tokens = max_completion_tokens
        self.structured_mode = structured_mode
        self.structured = StructuredCompletionClient(
            client,
            model,
            max_completion_tokens=max_completion_tokens,
        )
        self.adapter = ReviewAdapter()

    def review(self, request: ReviewRequest) -> ReviewResult:
        return self.review_batch(
            ReviewBatchRequest(
                events=(
                    ReviewBatchEvent(
                        event_id=request.event_id,
                        transcript_text=request.transcript_text,
                        final_response=request.final_response,
                        allowed_tools=request.allowed_tools,
                    ),
                ),
                current_user_profile=request.current_user_profile,
                current_decision_rules=request.current_decision_rules,
                current_decision_cards=request.current_decision_cards,
            )
        )

    def review_batch(self, request: ReviewBatchRequest) -> ReviewResult:
        if not request.events:
            return ReviewResult(actions=(), summary="No completed events to review.")
        offered_tools = self._offered_tools(request)
        if not offered_tools:
            return ReviewResult(actions=(), summary="No management tools were allowed.")

        content = self._review_input(request)
        feedback: str | None = None
        invalid_reasons: list[str] = []
        raw_attempts: list[object] = []
        for attempt in range(2):
            attempt_mode = self.structured_mode
            if self.structured_mode == "auto":
                attempt_mode = "tools" if attempt == 0 else "json"
            attempt_content = content
            if feedback is not None:
                attempt_content += (
                    "\n\n<validation_feedback>\n"
                    f"{feedback}\n"
                    "Return a complete corrected result for the same batch.\n"
                    "</validation_feedback>"
                )
            try:
                calls = self.structured.call_once(
                    system=DREAM_COMBINED_REVIEW_PROMPT,
                    content=attempt_content,
                    tools=offered_tools,
                    forced_tool="review_batch_result",
                    mode=attempt_mode,
                    allow_empty=False,
                )
                raw_attempts.append(self._trace_value(calls))
                canonical = self.adapter.adapt(
                    calls,
                    self._adapter_context(request),
                )
            except StructuredProviderError as exc:
                category = str(exc)
                return ReviewResult(
                    actions=(),
                    summary=f"Background review provider failed ({category}).",
                    status="failed",
                    error=f"provider request failed ({category})",
                    trace={
                        "raw_llm_output": raw_attempts,
                        "adapter_error": f"provider request failed ({category})",
                    },
                )
            except (
                InvalidReviewOutput,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
            ) as exc:
                reason = self._safe_failure_reason(exc)
                invalid_reasons.append(reason)
                if attempt == 0:
                    feedback = reason
                    continue
                return self._invalid_result(
                    after_repair=True,
                    attempt_reasons=tuple(invalid_reasons),
                    trace={
                        "raw_llm_output": raw_attempts,
                        "adapter_input": raw_attempts,
                        "adapter_error": reason,
                    },
                )
            except Exception:
                return ReviewResult(
                    actions=(),
                    summary="Background review provider failed.",
                    status="failed",
                    error="provider request failed",
                )
            pending_skill_count = self._pending_skill_count(canonical)
            return ReviewResult(
                actions=canonical.actions,
                summary=self._summary(canonical, pending_skill_count),
                event_dispositions=canonical.event_dispositions,
                trace=self._review_trace(calls, canonical),
            )
        return self._invalid_result(after_repair=True)

    @staticmethod
    def _pending_skill_count(canonical) -> int:
        proposal = canonical.knowledge_proposal or {}
        candidates = proposal.get("knowledge_candidates", [])
        if not isinstance(candidates, list):
            return 0
        return sum(
            isinstance(candidate, dict)
            and candidate.get("type") == "workflow_skill"
            and candidate.get("status") == "pending_skill_implementation"
            for candidate in candidates
        )

    @staticmethod
    def _summary(canonical, pending_skill_count: int) -> str:
        action_count = len(canonical.actions)
        if action_count and pending_skill_count:
            return (
                f"LLM review proposed {action_count} management action(s) and "
                f"recorded {pending_skill_count} pending skill candidate(s)."
            )
        if action_count:
            return f"LLM review proposed {action_count} management action(s)."
        if pending_skill_count:
            return (
                f"LLM review recorded {pending_skill_count} pending skill "
                "candidate(s)."
            )
        return "Nothing to save."

    @classmethod
    def _review_trace(cls, raw: object, canonical) -> dict[str, object]:
        raw_value = cls._trace_value(raw)
        actions = [
            {
                "kind": action.kind.value,
                "tool_name": action.tool_name,
                "payload": action.payload,
                "source_event_ids": list(action.evidence_event_ids),
            }
            for action in canonical.actions
        ]
        proposal = canonical.knowledge_proposal or {"knowledge_candidates": []}
        return {
            "raw_llm_output": raw_value,
            "adapter_input": raw_value,
            "adapter_output": proposal,
            "knowledge_proposal": proposal,
            "adapter_diagnostics": canonical.adapter_diagnostics or {},
            "canonical_review": {"actions": actions},
        }

    @classmethod
    def _trace_value(cls, value: object) -> object:
        if isinstance(value, StructuredToolCall):
            return {"name": value.name, "arguments": cls._trace_value(value.arguments)}
        if isinstance(value, dict):
            return {str(key): cls._trace_value(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [cls._trace_value(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return repr(value)

    @staticmethod
    def _invalid_result(
        *,
        after_repair: bool,
        attempt_reasons: tuple[str, ...] = (),
        trace: dict[str, object] | None = None,
    ) -> ReviewResult:
        suffix = " after one repair" if after_repair else ""
        labels = ("Initial attempt", "Repair attempt")
        reason_suffix = "".join(
            f" {labels[index]}: {reason.rstrip('.')}."
            for index, reason in enumerate(attempt_reasons[:2])
        )
        error_code = next(
            (
                "INVALID_REPLACE_TARGET"
                for reason in reversed(attempt_reasons)
                if reason.startswith("INVALID_REPLACE_TARGET")
            ),
            None,
        )
        return ReviewResult(
            actions=(),
            summary=(
                f"LLM review returned invalid structured output{suffix}.{reason_suffix}"
            ),
            status="failed",
            error=error_code or "invalid structured output",
            trace=trace,
        )

    @staticmethod
    def _safe_failure_reason(exc: Exception) -> str:
        if isinstance(exc, InvalidReviewOutput):
            return str(exc)
        if isinstance(exc, json.JSONDecodeError):
            return "The JSON payload could not be decoded."
        if isinstance(exc, ValueError) and str(exc) in {
            "no choices",
            "no structured tool calls",
            "invalid structured tool call",
            "structured output truncated",
            "unexpected forced tool",
        }:
            return str(exc)
        return f"{type(exc).__name__} while validating structured output"

    @staticmethod
    def _offered_tools(request: ReviewBatchRequest) -> tuple[dict[str, object], ...]:
        allowed = {tool for event in request.events for tool in event.allowed_tools}
        if not allowed.intersection(
            {"memory_manage", "decision_card_manage", "skill_manage"}
        ):
            return ()
        return (_COMBINED_RESULT_TOOL,)

    @staticmethod
    def _adapter_context(request: ReviewBatchRequest) -> ReviewAdapterContext:
        allowed_by_event = {
            event.event_id: event.allowed_tools for event in request.events
        }
        allowed = {tool for tools in allowed_by_event.values() for tool in tools}
        return ReviewAdapterContext(
            event_ids=tuple(event.event_id for event in request.events),
            existing_memory=request.current_user_profile,
            existing_decision_cards=request.current_decision_cards,
            allowed_tools_by_event=allowed_by_event,
            require_initial_memory=(
                not request.current_user_profile.strip() and "memory_manage" in allowed
            ),
            require_initial_decision_card=(
                not request.current_decision_rules.strip()
                and not request.current_decision_cards
                and "decision_card_manage" in allowed
            ),
        )

    @staticmethod
    def _review_input(request: ReviewBatchRequest) -> str:
        conversations: list[str] = ["<completed_conversations>"]
        for event in request.events:
            conversations.extend(
                [
                    f"<conversation event_id={json.dumps(event.event_id)}>",
                    event.transcript_text,
                    "<foreground_final_response>",
                    event.final_response,
                    "</foreground_final_response>",
                    "</conversation>",
                ]
            )
        conversations.append("</completed_conversations>")
        allowed = {tool for event in request.events for tool in event.allowed_tools}
        bootstrap_requirements: list[str] = []
        if not request.current_user_profile.strip() and "memory_manage" in allowed:
            bootstrap_requirements.append(
                "The initial user profile is empty. If this batch contains durable "
                "user evidence, include a user_preference knowledge candidate."
            )
        if (
            not request.current_decision_rules.strip()
            and not request.current_decision_cards
            and "decision_card_manage" in allowed
        ):
            bootstrap_requirements.append(
                "The initial AI decision card collection is empty. If this batch "
                "contains reusable decision evidence, include a decision_rule "
                "knowledge candidate. Do not invent one when the evidence does not "
                "support it."
            )
        if bootstrap_requirements:
            conversations.extend(
                [
                    "<initial_candidate_requirements>",
                    *bootstrap_requirements,
                    "Do not invent evidence to satisfy these requirements.",
                    "</initial_candidate_requirements>",
                ]
            )
        current_cards = "\n\n".join(request.current_decision_cards) or "(none)"
        current_memory_items = [
            {"memory_id": item.memory_id, "content": item.content}
            for item in parse_memory_items(request.current_user_profile)
        ]
        conversations.extend(
            [
                "<current_user_profile_items>",
                (
                    json.dumps(current_memory_items, ensure_ascii=False, indent=2)
                    if current_memory_items
                    else "(empty)"
                ),
                "</current_user_profile_items>",
                "<current_decision_rules>",
                request.current_decision_rules or "(empty)",
                "</current_decision_rules>",
                "<current_decision_cards>",
                current_cards,
                "</current_decision_cards>",
            ]
        )
        return "\n".join(conversations)
