import importlib
import json

import pytest

from dream.extraction.structured import StructuredToolCall


def _adapter_api():
    module = importlib.import_module("dream.extraction.provider_adapter")
    context = module.ReviewAdapterContext(
        event_ids=("evt-1", "evt-2"),
        existing_memory=(
            "Prefers concise responses.\n<!-- dream-source: evt-old -->\n"
            "§\nRequires verified bank channels.\n"
            "<!-- dream-source: evt-old-2 -->\n"
        ),
        existing_decision_cards=(),
        allowed_tools_by_event={
            "evt-1": frozenset({"memory_manage", "decision_card_manage"}),
            "evt-2": frozenset({"memory_manage", "decision_card_manage"}),
        },
    )
    return module, module.ReviewAdapter(), context


def _standard_payload() -> dict[str, object]:
    return {
        "memory_actions": [
            {
                "action": "add",
                "content": "Prefers explicit risk categories.",
                "confidence": 0.76,
                "source_event_ids": ["evt-1"],
            }
        ],
        "decision_card_actions": [
            {
                "id": "verify-before-risky-action",
                "title": "Verify before risky action",
                "scenario": "A risky action is requested.",
                "signals": ["irreversible change"],
                "principle": "Verify before applying it.",
                "outcome": "Avoided an unsafe action.",
                "boundaries": "Only when state is uncertain.",
                "confidence": 0.9,
                "source_event_ids": ["evt-2"],
            }
        ],
        "event_dispositions": [
            {"event_id": "evt-1", "disposition": "used", "reason": None},
            {"event_id": "evt-2", "disposition": "used", "reason": None},
        ],
        "nothing_to_save_reason": None,
    }


def test_adapter_accepts_standard_agnes_output() -> None:
    _, adapter, context = _adapter_api()
    raw = (
        StructuredToolCall(
            name="review_batch_result",
            arguments=_standard_payload(),
        ),
    )

    result = adapter.adapt(raw, context)

    assert len(result.memory_actions) == 1
    assert result.memory_actions[0].payload["confidence"] == 0.76
    assert len(result.decision_card_actions) == 1
    assert len(result.event_dispositions) == 2


def test_adapter_wraps_single_memory_object_and_maps_field_aliases() -> None:
    _, adapter, context = _adapter_api()
    raw = {
        "memory_actions": {
            "operation": "add",
            "content": "Prefers explicit escalation conditions.",
            "sourceEvents": "evt-1",
        },
        "decision_card_actions": None,
    }

    result = adapter.adapt(raw, context)

    assert len(result.memory_actions) == 1
    action = result.memory_actions[0]
    assert action.payload["action"] == "add"
    assert action.evidence_event_ids == ("evt-1",)


def test_adapter_fills_missing_sources_from_current_event_batch() -> None:
    _, adapter, context = _adapter_api()
    raw = {
        "memory_actions": {
            "action": "add",
            "content": "Prefers explicit escalation conditions.",
        },
        "decision_card_actions": [],
    }

    result = adapter.adapt(raw, context)

    assert result.memory_actions[0].evidence_event_ids == ("evt-1", "evt-2")
    assert {
        disposition.event_id: disposition.disposition
        for disposition in result.event_dispositions
    } == {"evt-1": "used", "evt-2": "used"}


def test_adapter_extracts_markdown_wrapped_json() -> None:
    _, adapter, context = _adapter_api()
    raw = f"```json\n{json.dumps(_standard_payload())}\n```"

    result = adapter.adapt(raw, context)

    assert len(result.memory_actions) == 1
    assert len(result.decision_card_actions) == 1


def test_adapter_unwraps_result_and_ignores_extra_fields() -> None:
    _, adapter, context = _adapter_api()
    payload = _standard_payload()
    payload["provider_notes"] = "not part of DREAM canonical review"
    raw = {
        "review_batch_result": payload,
        "request_id": "provider-request-1",
    }

    result = adapter.adapt(raw, context)

    assert len(result.actions) == 2
    assert "provider_notes" not in result.memory_actions[0].payload


def test_adapter_rejects_replace_without_old_content() -> None:
    module, adapter, context = _adapter_api()
    raw = {
        "memory_actions": {
            "operation": "replace",
            "content": "Prefers concise responses for urgent finance.",
            "sources": ["evt-1"],
        },
        "decision_card_actions": [],
    }

    with pytest.raises(module.InvalidReviewOutput, match="old_content"):
        adapter.adapt(raw, context)


def test_adapter_converts_skill_action_to_canonical_review() -> None:
    _, adapter, context = _adapter_api()
    context = type(context)(
        event_ids=context.event_ids,
        existing_memory=context.existing_memory,
        existing_decision_cards=context.existing_decision_cards,
        allowed_tools_by_event={
            event_id: tools | {"skill_manage"}
            for event_id, tools in context.allowed_tools_by_event.items()
        },
    )
    raw = {
        "memory_actions": [],
        "decision_card_actions": [],
        "skill_actions": {
            "id": "employee-report-writing",
            "title": "Employee Report Writing",
            "scenario": "Prepare a monthly employee report.",
            "inputs": ["approved activity records"],
            "steps": ["collect", "verify", "render"],
            "output_template": "Summary, risks, next actions",
            "cautions": "Exclude unverified personal data.",
            "confidence": 0.9,
            "source_event_ids": ["evt-1"],
        },
    }

    result = adapter.adapt(raw, context)

    assert len(result.skill_actions) == 1
    assert result.skill_actions[0].kind.value == "skill"
    assert result.skill_actions[0].tool_name == "skill_manage"


def test_review_adapter_routes_knowledge_proposals() -> None:
    _, adapter, context = _adapter_api()
    raw = {
        "knowledge_candidates": [
            {
                "type": "user_preference",
                "content": "User prefers immediate and deferred actions separately.",
                "confidence": 0.9,
                "source_event_ids": ["evt-1"],
            },
            {
                "type": "decision_rule",
                "id": "batch-payment-handling",
                "content": "Split partially successful payment batches by status.",
                "confidence": 0.92,
                "source_event_ids": ["evt-2"],
            },
        ]
    }

    result = adapter.adapt(raw, context)

    assert len(result.memory_actions) == 1
    assert len(result.decision_card_actions) == 1
    assert result.decision_card_actions[0].payload["id"] == "batch-payment-handling"


def test_knowledge_mode_accepts_one_valid_type_during_bootstrap() -> None:
    module, adapter, context = _adapter_api()
    bootstrap = module.ReviewAdapterContext(
        event_ids=context.event_ids,
        existing_memory="",
        existing_decision_cards=(),
        allowed_tools_by_event=context.allowed_tools_by_event,
        require_initial_memory=True,
        require_initial_decision_card=True,
    )

    result = adapter.adapt(
        {
            "knowledge_candidates": [
                {
                    "type": "user_preference",
                    "content": "User prefers concise conclusions.",
                    "confidence": 0.9,
                    "source_event_ids": ["evt-1"],
                }
            ],
            "nothing_to_save_reason": None,
        },
        bootstrap,
    )

    assert len(result.memory_actions) == 1
    assert result.decision_card_actions == ()


def test_empty_knowledge_proposal_requires_explicit_nothing_reason() -> None:
    module, adapter, context = _adapter_api()

    with pytest.raises(module.InvalidReviewOutput, match="nothing_to_save_reason"):
        adapter.adapt(
            {"knowledge_candidates": [], "nothing_to_save_reason": None},
            context,
        )


def _knowledge_context(module, context):
    return module.ReviewAdapterContext(
        event_ids=context.event_ids,
        existing_memory=context.existing_memory,
        existing_decision_cards=context.existing_decision_cards,
        allowed_tools_by_event={
            event_id: tools | {"skill_manage"}
            for event_id, tools in context.allowed_tools_by_event.items()
        },
    )


def _grouped_knowledge_payload() -> dict[str, object]:
    return {
        "knowledge_candidates": {
            "user_persona": [
                {
                    "content": "User prefers explicit continuation conditions.",
                    "confidence": 0.9,
                    "source_event_ids": ["evt-1"],
                }
            ],
            "decision_rules": [
                {
                    "id": "verify-beneficiary-change",
                    "name": "Verify beneficiary changes",
                    "content": "Verify beneficiary changes through an independent channel.",
                    "confidence": 0.94,
                    "source_event_ids": ["evt-1"],
                    "trigger": "A beneficiary account changes.",
                    "signals": ["new account"],
                    "constraints": ["Do not rely on the change message itself."],
                    "outcome": "Avoided an unauthorized payment.",
                }
            ],
            "skills": [
                {
                    "id": "risk-report-writing",
                    "name": "Risk Report Writing",
                    "content": "Produce a one-page evidence-backed risk report.",
                    "confidence": 0.91,
                    "source_event_ids": ["evt-2"],
                    "trigger": "A management risk report is requested.",
                    "inputs": ["verified incidents"],
                    "steps": ["Summarize", "Classify", "Assign actions"],
                    "output_template": "Conclusion, risks, measures, pending decisions",
                    "constraints": ["Label uncertain data."],
                }
            ],
        },
        "event_dispositions": [
            {"event_id": "evt-1", "disposition": "used", "reason": None},
            {"event_id": "evt-2", "disposition": "used", "reason": None},
        ],
        "nothing_to_save_reason": None,
    }


def test_adapter_unwraps_name_and_parameters_object() -> None:
    module, adapter, context = _adapter_api()
    context = _knowledge_context(module, context)
    raw = (
        StructuredToolCall(
            name="review_batch_result",
            arguments={
                "name": "review_batch_result",
                "parameters": _grouped_knowledge_payload(),
            },
        ),
    )

    result = adapter.adapt(raw, context)

    assert len(result.actions) == 2
    assert result.adapter_diagnostics["provider_shape"] == "tool_call+name+parameters"
    assert result.adapter_diagnostics["wrapper_name"] == "review_batch_result"


def test_adapter_unwraps_name_and_parameters_json_string() -> None:
    module, adapter, context = _adapter_api()
    context = _knowledge_context(module, context)
    raw = {
        "name": "review_batch_result",
        "parameters": json.dumps(_grouped_knowledge_payload()),
    }

    result = adapter.adapt(raw, context)

    assert len(result.actions) == 2
    assert "decoded parameters JSON" in result.adapter_diagnostics[
        "normalization_changes"
    ]


def test_adapter_unwraps_name_and_arguments_object() -> None:
    module, adapter, context = _adapter_api()
    context = _knowledge_context(module, context)

    result = adapter.adapt(
        {
            "name": "review_batch_result",
            "arguments": _grouped_knowledge_payload(),
        },
        context,
    )

    assert len(result.actions) == 2
    assert "unwrapped arguments" in result.adapter_diagnostics[
        "normalization_changes"
    ]


def test_adapter_rejects_non_empty_unrecognized_payload() -> None:
    module, adapter, context = _adapter_api()

    with pytest.raises(
        module.InvalidReviewOutput,
        match="non-empty provider payload contained no recognized review fields",
    ):
        adapter.adapt({"provider_specific": "value"}, context)


def test_adapter_preserves_all_grouped_knowledge_categories() -> None:
    module, adapter, context = _adapter_api()
    context = _knowledge_context(module, context)

    result = adapter.adapt(_grouped_knowledge_payload(), context)

    assert len(result.memory_actions) == 1
    assert len(result.decision_card_actions) == 1
    assert result.skill_actions == ()
    candidates = result.knowledge_proposal["knowledge_candidates"]
    assert candidates[2]["status"] == "pending_skill_implementation"
    assert result.adapter_diagnostics["candidate_counts"] == {
        "before": 3,
        "after": 3,
        "persona": 1,
        "decision": 1,
        "skill": 1,
    }
    assert result.adapter_diagnostics["routed_action_counts"] == {
        "persona": 1,
        "decision": 1,
        "skill": 0,
    }


def test_skill_only_candidate_is_audited_without_management_action() -> None:
    module, adapter, context = _adapter_api()
    context = _knowledge_context(module, context)
    payload = {
        "knowledge_candidates": {
            "user_persona": [],
            "decision_rules": [],
            "skills": [
                {
                    "id": "risk-report-writing",
                    "name": "Risk Report Writing",
                    "content": "Produce an evidence-backed risk report.",
                    "confidence": 0.91,
                    "source_event_ids": ["evt-2"],
                    "trigger": "A management risk report is requested.",
                    "inputs": ["verified incidents"],
                    "steps": ["Summarize", "Classify", "Assign actions"],
                    "output_template": "Conclusion, risks, measures",
                    "constraints": ["Label uncertain data."],
                }
            ],
        },
        "event_dispositions": [
            {
                "event_id": "evt-1",
                "disposition": "no_durable_signal",
                "reason": "No durable signal.",
            },
            {"event_id": "evt-2", "disposition": "used", "reason": None},
        ],
        "nothing_to_save_reason": None,
    }

    result = adapter.adapt(payload, context)

    assert result.actions == ()
    assert result.nothing_to_save_reason is None
    assert result.knowledge_proposal["knowledge_candidates"][0]["status"] == (
        "pending_skill_implementation"
    )
    assert {
        item.event_id: item.disposition for item in result.event_dispositions
    } == {"evt-1": "no_durable_signal", "evt-2": "used"}


def test_adapter_ignores_extra_outer_fields_around_parameters() -> None:
    module, adapter, context = _adapter_api()
    context = _knowledge_context(module, context)
    raw = {
        "name": "review_batch_result",
        "parameters": _grouped_knowledge_payload(),
        "request_id": "provider-request-1",
        "provider_metadata": {"region": "test"},
    }

    result = adapter.adapt(raw, context)

    assert len(result.actions) == 2
    assert result.adapter_diagnostics["recognized_fields"] == [
        "event_dispositions",
        "knowledge_candidates",
        "nothing_to_save_reason",
    ]
