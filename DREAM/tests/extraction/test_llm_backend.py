import json
import hashlib
from types import SimpleNamespace

from dream.extraction.llm_backend import OpenAIReviewBackend
from dream.extraction.provider_adapter import CanonicalReview
from dream.extraction.models import (
    ArtifactKind,
    ReviewBatchEvent,
    ReviewBatchRequest,
    ReviewRequest,
    ReviewEventDisposition,
)


class FakeCompletions:
    def __init__(self, tool_calls: list[object]) -> None:
        self.tool_calls = tool_calls
        self.kwargs: dict[str, object] = {}
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        self.calls.append(kwargs)
        message = SimpleNamespace(tool_calls=self.tool_calls, content=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _tool_call(name: str, arguments: dict[str, object]) -> object:
    return SimpleNamespace(
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments))
    )


def _memory_id(content: str) -> str:
    return "mem-" + hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def _combined_call(
    *,
    memory_actions: list[dict[str, object]] | None = None,
    decision_card_actions: list[dict[str, object]] | None = None,
    nothing_to_save_reason: str | None = None,
    event_ids: tuple[str, ...] | None = None,
) -> object:
    memory_actions = memory_actions or []
    decision_card_actions = decision_card_actions or []
    referenced = {
        source
        for action in (*memory_actions, *decision_card_actions)
        for source in action.get("source_event_ids", [])
        if isinstance(source, str)
    }
    disposition_ids = event_ids if event_ids is not None else tuple(sorted(referenced))
    return _tool_call(
        "review_batch_result",
        {
            "memory_actions": memory_actions,
            "decision_card_actions": decision_card_actions,
            "event_dispositions": [
                {
                    "event_id": event_id,
                    "disposition": (
                        "used" if event_id in referenced else "no_durable_signal"
                    ),
                    "reason": (
                        None
                        if event_id in referenced
                        else "No durable user or decision signal in this event."
                    ),
                }
                for event_id in disposition_ids
            ],
            "nothing_to_save_reason": nothing_to_save_reason,
        },
    )


def test_llm_backend_delegates_raw_output_to_review_adapter() -> None:
    completions = FakeCompletions(
        [_tool_call("review_batch_result", {"provider_specific": "value"})]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    class CapturingAdapter:
        def __init__(self) -> None:
            self.raw: object | None = None
            self.context: object | None = None

        def adapt(self, raw: object, context: object) -> CanonicalReview:
            self.raw = raw
            self.context = context
            return CanonicalReview(
                memory_actions=(),
                decision_card_actions=(),
                event_dispositions=(
                    ReviewEventDisposition(
                        event_id="evt-adapter",
                        disposition="no_durable_signal",
                        reason="No durable signal.",
                    ),
                ),
            )

    adapter = CapturingAdapter()
    backend.adapter = adapter

    result = backend.review(
        ReviewRequest(
            event_id="evt-adapter",
            transcript_text="No durable preference.",
            final_response="Acknowledged.",
            allowed_tools=frozenset({"memory_manage"}),
            current_user_profile="Existing profile.",
        )
    )

    assert result.status == "success"
    assert adapter.raw is not None
    assert adapter.context is not None


def test_llm_backend_uses_one_forced_combined_result_tool() -> None:
    completions = FakeCompletions(
        [
            _combined_call(
                memory_actions=[
                    {
                        "action": "add",
                        "content": "Prefers concise answers.",
                        "source_event_ids": ["evt-combined"],
                    }
                ],
                decision_card_actions=[
                    {
                        "id": "verify-before-risky-action",
                        "title": "Verify before risky action",
                        "scenario": "A risky action is requested.",
                        "signals": ["irreversible change"],
                        "principle": "Verify before applying it.",
                        "outcome": "Avoided an unsafe duplicate action.",
                        "boundaries": "Only when the original state is uncertain.",
                        "confidence": 0.9,
                        "source_event_ids": ["evt-combined"],
                    }
                ],
            )
        ]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review(
        ReviewRequest(
            event_id="evt-combined",
            transcript_text="user: concise please",
            final_response="Verified before acting.",
            allowed_tools=frozenset({"memory_manage", "decision_card_manage"}),
        )
    )

    assert result.status == "success"
    assert {action.kind for action in result.actions} == {
        ArtifactKind.USER_PROFILE,
        ArtifactKind.DECISION_CARD,
    }
    assert [tool["function"]["name"] for tool in completions.calls[0]["tools"]] == [
        "review_batch_result"
    ]
    assert completions.calls[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "review_batch_result"},
    }


def test_llm_backend_parses_both_priority_management_tools() -> None:
    completions = FakeCompletions(
        [
            _combined_call(
                memory_actions=[
                    {
                        "action": "add",
                        "content": "Prefers concise answers.",
                        "source_event_ids": ["evt-llm"],
                    }
                ],
                decision_card_actions=[
                    {
                        "id": "verify-before-risky-action",
                        "title": "高风险操作前先验证",
                        "scenario": "用户要求执行不可逆操作",
                        "signals": ["不可逆"],
                        "principle": "先验证，再执行。",
                        "outcome": "避免错误修改。",
                        "boundaries": "低风险操作除外。",
                        "confidence": 0.86,
                        "source_event_ids": ["evt-llm"],
                    }
                ],
            )
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    backend = OpenAIReviewBackend(client=client, model="review-model")

    result = backend.review(
        ReviewRequest(
            event_id="evt-llm",
            transcript_text="User: please be concise",
            final_response="Understood.",
            allowed_tools=frozenset({"memory_manage", "decision_card_manage"}),
            current_user_profile="",
            current_decision_rules="",
            current_decision_cards=(),
        )
    )

    assert {action.kind for action in result.actions} == {
        ArtifactKind.USER_PROFILE,
        ArtifactKind.DECISION_CARD,
    }
    assert all(action.source_event_id == "evt-llm" for action in result.actions)
    assert completions.kwargs["model"] == "review-model"
    assert [tool["function"]["name"] for tool in completions.kwargs["tools"]] == [
        "review_batch_result"
    ]


def test_llm_backend_does_not_offer_a_disallowed_management_tool() -> None:
    completions = FakeCompletions([])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    backend = OpenAIReviewBackend(client=client, model="review-model")

    backend.review(
        ReviewRequest(
            event_id="evt-profile-only",
            transcript_text="User: remember this preference",
            final_response="Saved.",
            allowed_tools=frozenset({"memory_manage"}),
        )
    )

    assert [tool["function"]["name"] for tool in completions.calls[0]["tools"]] == [
        "review_batch_result",
    ]


def test_llm_review_keeps_user_facts_out_of_shared_ai_cards() -> None:
    completions = FakeCompletions([])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    backend = OpenAIReviewBackend(client=client, model="review-model")

    backend.review(
        ReviewRequest(
            event_id="evt-private-user-fact",
            transcript_text="User: my private preference is concise answers",
            final_response="Understood.",
            allowed_tools=frozenset({"memory_manage", "decision_card_manage"}),
        )
    )

    system_prompt = completions.kwargs["messages"][0]["content"]
    assert "Never copy a user's personal facts" in system_prompt
    assert "user-agnostic" in system_prompt


def test_llm_review_accepts_json_only_provider() -> None:
    class JsonOnlyCompletions:
        def create(self, **kwargs: object) -> object:
            if "tools" in kwargs:
                raise RuntimeError("tools unsupported")
            content = json.dumps(
                {
                    "tool_calls": [
                        {
                            "name": "review_batch_result",
                            "arguments": {
                                "memory_actions": [
                                    {
                                        "action": "add",
                                        "content": "Prefers concise answers.",
                                        "source_event_ids": ["evt-json"],
                                    }
                                ],
                                "decision_card_actions": [],
                                "event_dispositions": [
                                    {
                                        "event_id": "evt-json",
                                        "disposition": "used",
                                        "reason": None,
                                    }
                                ],
                                "nothing_to_save_reason": None,
                            },
                        }
                    ]
                }
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=JsonOnlyCompletions()))
    backend = OpenAIReviewBackend(
        client=client,
        model="agnes-model",
        structured_mode="json",
    )

    result = backend.review(
        ReviewRequest(
            event_id="evt-json",
            transcript_text="User: concise please",
            final_response="Understood.",
            allowed_tools=frozenset({"memory_manage"}),
        )
    )

    assert result.status == "success"
    assert result.actions[0].kind is ArtifactKind.USER_PROFILE


def _batch_request(*, bootstrap: bool = False) -> ReviewBatchRequest:
    return ReviewBatchRequest(
        events=(
            ReviewBatchEvent(
                event_id="evt-1",
                transcript_text="user: I prefer concise answers",
                final_response="Understood.",
                allowed_tools=frozenset({"memory_manage", "decision_card_manage"}),
            ),
            ReviewBatchEvent(
                event_id="evt-2",
                transcript_text="assistant: verify before risky action",
                final_response="Verified.",
                allowed_tools=frozenset({"memory_manage", "decision_card_manage"}),
            ),
        ),
        current_user_profile="" if bootstrap else "Existing user profile.",
        current_decision_rules="" if bootstrap else "Existing decision rules.",
    )


def _incremental_memory_request() -> ReviewBatchRequest:
    return ReviewBatchRequest(
        events=(
            ReviewBatchEvent(
                event_id="evt-6",
                transcript_text="user: concise answers are especially important for urgent finance",
                final_response="Understood.",
                allowed_tools=frozenset({"memory_manage"}),
            ),
        ),
        current_user_profile=(
            "Prefers concise responses.\n<!-- dream-source: evt-1 -->\n"
            "§\n"
            "Requires verified bank channels.\n<!-- dream-source: evt-2 -->\n"
        ),
        current_decision_rules="Existing decision rules.",
    )


class SequencedCompletions:
    def __init__(self, responses: list[list[object] | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        message = SimpleNamespace(tool_calls=response, content=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_batch_review_calls_agnes_once_and_preserves_all_evidence_ids() -> None:
    completions = SequencedCompletions(
        [
            [
                _combined_call(
                    memory_actions=[
                        {
                            "action": "add",
                            "content": "Prefers concise answers.",
                            "source_event_ids": ["evt-1", "evt-2"],
                        }
                    ],
                )
            ]
        ]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "success"
    assert len(completions.calls) == 1
    assert completions.calls[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "review_batch_result"},
    }
    assert result.actions[0].evidence_event_ids == ("evt-1", "evt-2")
    prompt = str(completions.calls[0]["messages"][1]["content"])
    assert 'event_id="evt-1"' in prompt
    assert 'event_id="evt-2"' in prompt


def test_batch_schema_requires_an_explicit_disposition_for_every_event() -> None:
    completions = SequencedCompletions(
        [
            [
                _combined_call(
                    nothing_to_save_reason="No durable data.",
                    event_ids=("evt-1", "evt-2"),
                )
            ]
        ]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "success"
    parameters = completions.calls[0]["tools"][0]["function"]["parameters"]
    dispositions = parameters["properties"]["event_dispositions"]
    assert dispositions["type"] == "array"
    assert set(dispositions["items"]["properties"]["disposition"]["enum"]) == {
        "used",
        "no_durable_signal",
    }
    assert "event_dispositions" in parameters["required"]
    prompt = str(completions.calls[0]["messages"][0]["content"])
    assert "Never merge" in prompt
    assert "distinct facts or decision principles" in prompt
    assert "exactly once in event_dispositions" in prompt


def test_used_disposition_with_reason_is_normalized_to_null() -> None:
    memory = {
        "action": "add",
        "content": "Prefers concise answers.",
        "source_event_ids": ["evt-1"],
    }
    call = _combined_call(
        memory_actions=[memory],
        event_ids=("evt-1", "evt-2"),
    )
    payload = json.loads(call.function.arguments)
    payload["event_dispositions"][0]["reason"] = "This event supports the memory."
    completions = SequencedCompletions([[_tool_call("review_batch_result", payload)]])
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "success"
    assert len(completions.calls) == 1
    disposition = next(
        item for item in result.event_dispositions if item.event_id == "evt-1"
    )
    assert disposition.disposition == "used"
    assert disposition.reason is None


def test_nested_review_result_ignores_unknown_top_level_fields() -> None:
    memory = {
        "action": "add",
        "content": "Prefers concise answers.",
        "source_event_ids": ["evt-1"],
    }
    direct = _combined_call(
        memory_actions=[memory],
        event_ids=("evt-1", "evt-2"),
    )
    inner = json.loads(direct.function.arguments)
    inner["extra_field"] = "provider commentary"
    wrapped = {
        "review_batch_result": inner,
        "outer_extra": "ignored",
    }
    completions = SequencedCompletions([[_tool_call("review_batch_result", wrapped)]])
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "success"
    assert len(result.actions) == 1
    assert len(completions.calls) == 1


def test_missing_event_dispositions_are_derived_from_action_evidence() -> None:
    payload = {
        "memory_actions": [
            {
                "action": "add",
                "content": "Prefers concise answers.",
                "source_event_ids": ["evt-1"],
            }
        ],
        "decision_card_actions": [],
        "nothing_to_save_reason": None,
    }
    call = _tool_call("review_batch_result", payload)
    completions = SequencedCompletions([[call], [call]])
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "success"
    assert len(completions.calls) == 1
    assert {item.event_id: item.disposition for item in result.event_dispositions} == {
        "evt-1": "used",
        "evt-2": "no_durable_signal",
    }


def test_markdown_fenced_json_review_result_is_parsed_and_validated() -> None:
    class MarkdownJsonCompletions:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def create(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            payload = {
                "memory_actions": [
                    {
                        "action": "add",
                        "content": "Prefers explicit risk categories.",
                        "source_event_ids": ["evt-6"],
                    }
                ],
                "decision_card_actions": [],
                "event_dispositions": [
                    {
                        "event_id": "evt-6",
                        "disposition": "used",
                        "reason": None,
                    }
                ],
                "nothing_to_save_reason": None,
            }
            content = f"```json\n{json.dumps(payload)}\n```"
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    completions = MarkdownJsonCompletions()
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="json",
    )

    result = backend.review_batch(_incremental_memory_request())

    assert result.status == "success"
    assert len(completions.calls) == 1
    assert result.actions[0].payload["content"] == "Prefers explicit risk categories."


def test_replace_without_old_content_remains_invalid_after_one_repair() -> None:
    invalid = {
        "action": "replace",
        "memory_id": _memory_id("Prefers concise responses."),
        "content": "Prefers concise responses for urgent financial operations.",
        "source_event_ids": ["evt-6"],
    }
    call = _combined_call(memory_actions=[invalid], event_ids=("evt-6",))
    completions = SequencedCompletions([[call], [call]])
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_incremental_memory_request())

    assert result.status == "failed"
    assert result.error == "invalid structured output"
    assert "old_content must be non-empty text" in result.summary
    assert len(completions.calls) == 2


def test_review_schema_exposes_knowledge_candidates_not_storage_actions() -> None:
    completions = SequencedCompletions(
        [
            [
                _combined_call(
                    nothing_to_save_reason="No durable data.", event_ids=("evt-6",)
                )
            ]
        ]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    backend.review_batch(_incremental_memory_request())

    parameters = completions.calls[0]["tools"][0]["function"]["parameters"]
    grouped = parameters["properties"]["knowledge_candidates"]
    assert grouped["properties"]["user_persona"]["items"]["properties"][
        "type"
    ]["enum"] == ["user_preference"]
    assert grouped["properties"]["decision_rules"]["items"]["properties"][
        "type"
    ]["enum"] == ["decision_rule"]
    assert grouped["properties"]["skills"]["items"]["properties"]["type"][
        "enum"
    ] == ["workflow_skill"]
    assert "memory_actions" not in parameters["properties"]
    system_prompt = str(completions.calls[0]["messages"][0]["content"])
    assert "DREAM, not you, decides where and how to store" in system_prompt


def test_incremental_prompt_lists_current_profile_as_atomic_memory_items() -> None:
    completions = SequencedCompletions(
        [
            [
                _combined_call(
                    nothing_to_save_reason="No durable data.", event_ids=("evt-6",)
                )
            ]
        ]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    backend.review_batch(_incremental_memory_request())

    prompt = str(completions.calls[0]["messages"][1]["content"])
    assert _memory_id("Prefers concise responses.") in prompt
    assert _memory_id("Requires verified bank channels.") in prompt
    assert '"content": "Prefers concise responses."' in prompt
    assert "<!-- dream-source:" not in prompt


def test_whole_profile_replace_gets_one_repair_to_an_atomic_memory_item() -> None:
    whole_profile = "Prefers concise responses.\n§\nRequires verified bank channels."
    invalid = {
        "action": "replace",
        "memory_id": _memory_id(whole_profile),
        "old_content": whole_profile,
        "content": whole_profile + "\n§\nPrefers explicit risk categories.",
        "source_event_ids": ["evt-6"],
    }
    repaired = {
        "action": "replace",
        "memory_id": _memory_id("Prefers concise responses."),
        "old_content": "Prefers concise responses.",
        "content": "Prefers concise responses for urgent financial operations.",
        "source_event_ids": ["evt-6"],
    }
    completions = SequencedCompletions(
        [
            [_combined_call(memory_actions=[invalid], event_ids=("evt-6",))],
            [_combined_call(memory_actions=[repaired], event_ids=("evt-6",))],
        ]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_incremental_memory_request())

    assert result.status == "success"
    assert len(completions.calls) == 2
    assert result.actions[0].payload["memory_id"] == repaired["memory_id"]
    repair_prompt = str(completions.calls[1]["messages"][1]["content"])
    assert "INVALID_REPLACE_TARGET" in repair_prompt


def test_missing_event_disposition_gets_one_complete_repair() -> None:
    memory = {
        "action": "add",
        "content": "Prefers concise answers.",
        "source_event_ids": ["evt-1"],
    }
    completions = SequencedCompletions(
        [
            [_combined_call(memory_actions=[memory])],
            [_combined_call(memory_actions=[memory], event_ids=("evt-1", "evt-2"))],
        ]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "success"
    assert len(completions.calls) == 2
    repair_prompt = str(completions.calls[1]["messages"][1]["content"])
    assert "Every batch event requires exactly one disposition" in repair_prompt


def test_used_disposition_must_be_backed_by_an_action_source() -> None:
    invalid = _combined_call(
        nothing_to_save_reason="No durable data.",
        event_ids=("evt-1", "evt-2"),
    )
    payload = json.loads(invalid.function.arguments)
    payload["event_dispositions"][0] = {
        "event_id": "evt-1",
        "disposition": "used",
        "reason": None,
    }
    invalid = _tool_call("review_batch_result", payload)
    completions = SequencedCompletions([[invalid], [invalid]])
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "failed"
    assert "used disposition requires action evidence" in result.summary
    assert len(completions.calls) == 2


def test_coverage_protocol_does_not_limit_distinct_actions() -> None:
    actions = [
        {
            "action": "add",
            "content": f"Distinct durable preference {index}.",
            "source_event_ids": ["evt-1"],
        }
        for index in range(4)
    ]
    completions = SequencedCompletions(
        [[_combined_call(memory_actions=actions, event_ids=("evt-1", "evt-2"))]]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "success"
    assert len(result.actions) == 4
    assert len(completions.calls) == 1


def test_invalid_batch_output_gets_exactly_one_repair_with_feedback() -> None:
    invalid = _combined_call(
        memory_actions=[
            {
                "action": "add",
                "content": "",
                "source_event_ids": ["evt-unknown"],
            }
        ],
    )
    repaired = _combined_call(
        memory_actions=[
            {
                "action": "add",
                "content": "Prefers concise answers.",
                "source_event_ids": ["evt-1"],
            }
        ],
        event_ids=("evt-1", "evt-2"),
    )
    completions = SequencedCompletions([[invalid], [repaired]])
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "success"
    assert len(completions.calls) == 2
    repair_prompt = str(completions.calls[1]["messages"][1]["content"])
    assert "validation_feedback" in repair_prompt


def test_initial_partial_bootstrap_gets_one_repair_for_missing_ai_card() -> None:
    memory = {
        "action": "add",
        "content": "Prefers concise answers.",
        "source_event_ids": ["evt-1"],
    }
    decision = {
        "id": "verify-before-risky-action",
        "title": "Verify before risky action",
        "scenario": "A risky action is requested.",
        "signals": ["irreversible change"],
        "principle": "Verify before applying it.",
        "outcome": "Avoided an unsafe duplicate action.",
        "boundaries": "Only when the original state is uncertain.",
        "confidence": 0.9,
        "source_event_ids": ["evt-1", "evt-2"],
    }
    completions = SequencedCompletions(
        [
            [_combined_call(memory_actions=[memory])],
            [_combined_call(memory_actions=[memory], decision_card_actions=[decision])],
        ]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request(bootstrap=True))

    assert result.status == "success"
    assert {action.kind for action in result.actions} == {
        ArtifactKind.USER_PROFILE,
        ArtifactKind.DECISION_CARD,
    }
    assert len(completions.calls) == 2
    initial_prompt = str(completions.calls[0]["messages"][1]["content"])
    assert "initial AI decision card" in initial_prompt
    repair_prompt = str(completions.calls[1]["messages"][1]["content"])
    assert "initial AI decision card" in repair_prompt


def test_initial_explicit_nothing_gets_one_repair_for_complete_bootstrap() -> None:
    explicit_nothing = _combined_call(
        nothing_to_save_reason="No durable data.",
        event_ids=("evt-1", "evt-2"),
    )
    memory = {
        "action": "add",
        "content": "Prefers concise answers.",
        "source_event_ids": ["evt-1"],
    }
    decision = {
        "id": "verify-before-risky-action",
        "title": "Verify before risky action",
        "scenario": "A risky action is requested.",
        "signals": ["irreversible change"],
        "principle": "Verify before applying it.",
        "outcome": "Avoided an unsafe duplicate action.",
        "boundaries": "Only when the original state is uncertain.",
        "confidence": 0.9,
        "source_event_ids": ["evt-1", "evt-2"],
    }
    completions = SequencedCompletions(
        [
            [explicit_nothing],
            [_combined_call(memory_actions=[memory], decision_card_actions=[decision])],
        ]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request(bootstrap=True))

    assert result.status == "success"
    assert len(completions.calls) == 2
    repair_prompt = str(completions.calls[1]["messages"][1]["content"])
    assert "cannot publish nothing_to_save" in repair_prompt


def test_implicit_empty_output_gets_one_repair_then_explicit_nothing_succeeds() -> None:
    explicit_nothing = _combined_call(
        nothing_to_save_reason="No durable user fact or reusable decision was present.",
        event_ids=("evt-1", "evt-2"),
    )
    completions = SequencedCompletions([[], [explicit_nothing]])
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "success"
    assert result.actions == ()
    assert len(completions.calls) == 2
    assert len(completions.calls) == 2
    assert "validation_feedback" in str(completions.calls[1]["messages"][1]["content"])


def test_auto_mode_repairs_invalid_tools_output_once_with_json() -> None:
    class ToolsThenJsonCompletions:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def create(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            if "tools" in kwargs:
                message = SimpleNamespace(tool_calls=[], content="ordinary text")
            else:
                content = json.dumps(
                    {
                        "tool_calls": [
                            {
                                "name": "review_batch_result",
                                "arguments": {
                                    "memory_actions": [
                                        {
                                            "action": "add",
                                            "content": "Prefers concise answers.",
                                            "source_event_ids": ["evt-1"],
                                        }
                                    ],
                                    "decision_card_actions": [],
                                    "event_dispositions": [
                                        {
                                            "event_id": "evt-1",
                                            "disposition": "used",
                                            "reason": None,
                                        },
                                        {
                                            "event_id": "evt-2",
                                            "disposition": "no_durable_signal",
                                            "reason": "No durable signal.",
                                        },
                                    ],
                                    "nothing_to_save_reason": None,
                                },
                            }
                        ]
                    }
                )
                message = SimpleNamespace(content=content)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    completions = ToolsThenJsonCompletions()
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="auto",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "success"
    assert len(completions.calls) == 2
    assert "tools" in completions.calls[0]
    assert "response_format" in completions.calls[1]
    assert "validation_feedback" in str(completions.calls[1]["messages"][1]["content"])


def test_combined_result_accepts_omitted_null_reason_when_actions_exist() -> None:
    payload = {
        "memory_actions": [
            {
                "action": "add",
                "content": "Prefers concise answers.",
                "source_event_ids": ["evt-1"],
            }
        ],
        "decision_card_actions": [],
        "event_dispositions": [
            {"event_id": "evt-1", "disposition": "used", "reason": None},
            {
                "event_id": "evt-2",
                "disposition": "no_durable_signal",
                "reason": "No durable signal.",
            },
        ],
    }
    call = SimpleNamespace(
        function=SimpleNamespace(
            name="review_batch_result",
            arguments=json.dumps(payload),
        )
    )
    completions = SequencedCompletions([[call]])
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "success"
    assert len(result.actions) == 1
    assert len(completions.calls) == 1


def test_two_implicit_empty_outputs_fail_after_two_total_calls() -> None:
    completions = SequencedCompletions([[], []])
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "failed"
    assert result.error == "invalid structured output"
    assert "Initial attempt: no structured tool calls" in result.summary
    assert "Repair attempt: no structured tool calls" in result.summary
    assert len(completions.calls) == 2


def test_explicit_nothing_cannot_be_mixed_with_mutating_tools() -> None:
    mutation = {
        "action": "add",
        "content": "Prefers concise answers.",
        "source_event_ids": ["evt-1"],
    }
    completions = SequencedCompletions(
        [
            [
                _combined_call(
                    memory_actions=[mutation],
                    nothing_to_save_reason="No durable data.",
                )
            ],
            [
                _combined_call(
                    nothing_to_save_reason="No durable data.",
                    event_ids=("evt-1", "evt-2"),
                )
            ],
        ]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "success"
    assert result.actions == ()


def test_knowledge_proposal_routes_persona_and_decision_and_audits_skill() -> None:
    payload = {
        "knowledge_candidates": [
            {
                "type": "user_preference",
                "content": "User prefers immediate and deferred actions separately.",
                "confidence": 0.9,
                "source_event_ids": ["evt-knowledge"],
            },
            {
                "type": "decision_rule",
                "id": "supplier-account-verification",
                "content": "Independently verify supplier account changes.",
                "confidence": 0.95,
                "source_event_ids": ["evt-knowledge"],
            },
            {
                "type": "workflow_skill",
                "id": "bank-report-writing",
                "name": "Bank Report Writing",
                "content": "Structure reports as background, risks, and actions.",
                "trigger": "A bank risk report is requested.",
                "steps": ["Summarize background", "Assess risks", "List actions"],
                "constraints": ["Use verified evidence"],
                "confidence": 0.9,
                "source_event_ids": ["evt-knowledge"],
            },
        ],
        "event_dispositions": [
            {"event_id": "evt-knowledge", "disposition": "used", "reason": None}
        ],
        "nothing_to_save_reason": None,
    }
    completions = SequencedCompletions([[_tool_call("review_batch_result", payload)]])
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review(
        ReviewRequest(
            event_id="evt-knowledge",
            transcript_text="durable preference, rule, and workflow",
            final_response="completed",
            allowed_tools=frozenset(
                {"memory_manage", "decision_card_manage", "skill_manage"}
            ),
            current_user_profile="Existing profile.",
            current_decision_rules="Existing rules.",
        )
    )

    assert [action.kind for action in result.actions] == [
        ArtifactKind.USER_PROFILE,
        ArtifactKind.DECISION_CARD,
    ]
    assert result.trace is not None
    candidates = result.trace["knowledge_proposal"]["knowledge_candidates"]
    assert len(candidates) == 3
    assert candidates[2]["status"] == "pending_skill_implementation"
    assert len(result.trace["canonical_review"]["actions"]) == 2
    assert result.trace["adapter_diagnostics"]["routed_action_counts"]["skill"] == 0


def test_skill_only_proposal_is_successfully_recorded_as_pending() -> None:
    payload = {
        "knowledge_candidates": {
            "user_persona": [],
            "decision_rules": [],
            "skills": [
                {
                    "id": "bank-report-writing",
                    "name": "Bank Report Writing",
                    "content": "Structure reports as background, risks, and actions.",
                    "trigger": "A bank risk report is requested.",
                    "inputs": ["verified evidence"],
                    "steps": ["Summarize", "Assess", "Assign actions"],
                    "output_template": "Background, risks, actions",
                    "constraints": ["Use verified evidence"],
                    "confidence": 0.9,
                    "source_event_ids": ["evt-skill"],
                }
            ],
        },
        "event_dispositions": [
            {"event_id": "evt-skill", "disposition": "used", "reason": None}
        ],
        "nothing_to_save_reason": None,
    }
    completions = SequencedCompletions([[_tool_call("review_batch_result", payload)]])
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review(
        ReviewRequest(
            event_id="evt-skill",
            transcript_text="A reusable report workflow was completed.",
            final_response="Report delivered.",
            allowed_tools=frozenset({"skill_manage"}),
        )
    )

    assert result.status == "success"
    assert result.actions == ()
    assert "pending skill candidate" in result.summary
    assert result.trace is not None
    candidate = result.trace["knowledge_proposal"]["knowledge_candidates"][0]
    assert candidate["status"] == "pending_skill_implementation"
    assert result.trace["canonical_review"]["actions"] == []
    assert backend.prompt_version == "knowledge-extraction-v6-persona-canonicalization"


def test_knowledge_prompt_requires_per_event_multidimensional_coverage() -> None:
    completions = SequencedCompletions(
        [
            [
                _combined_call(
                    nothing_to_save_reason="No durable knowledge.",
                    event_ids=("evt-1", "evt-2"),
                )
            ]
        ]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    backend.review_batch(_batch_request())

    prompt = str(completions.calls[0]["messages"][0]["content"])
    assert "Inspect every event independently" in prompt
    assert "explicit durable user request" in prompt
    assert "Do not merge candidates with different business triggers" in prompt
    assert "may produce more than one knowledge type" in prompt
    assert "ordered, reusable procedure" in prompt


def test_knowledge_schema_requires_explicit_three_category_arrays() -> None:
    completions = SequencedCompletions(
        [
            [
                _combined_call(
                    nothing_to_save_reason="No durable knowledge.",
                    event_ids=("evt-1", "evt-2"),
                )
            ]
        ]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    backend.review_batch(_batch_request())

    parameters = completions.calls[0]["tools"][0]["function"]["parameters"]
    grouped = parameters["properties"]["knowledge_candidates"]
    assert grouped["type"] == "object"
    assert grouped["required"] == ["user_persona", "decision_rules", "skills"]
    assert set(grouped["properties"]) == {
        "user_persona",
        "decision_rules",
        "skills",
    }
    assert grouped["properties"]["user_persona"]["type"] == "array"
    assert grouped["properties"]["decision_rules"]["type"] == "array"
    assert grouped["properties"]["skills"]["type"] == "array"

    persona_properties = grouped["properties"]["user_persona"]["items"][
        "properties"
    ]
    assert {
        "target_memory_id",
        "statement",
        "new_information",
        "evidence",
        "merge_type",
    }.issubset(persona_properties)


def test_knowledge_prompt_requires_three_pass_checks_without_fabrication() -> None:
    completions = SequencedCompletions(
        [
            [
                _combined_call(
                    nothing_to_save_reason="No durable knowledge.",
                    event_ids=("evt-1", "evt-2"),
                )
            ]
        ]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    backend.review_batch(_batch_request())

    prompt = str(completions.calls[0]["messages"][0]["content"])
    assert "Pass 1 — User Persona Candidates" in prompt
    assert "Pass 2 — Decision Candidates" in prompt
    assert "Pass 3 — Skill Candidates" in prompt
    assert "Do not stop after finding the first knowledge category" in prompt
    assert "An empty category must still be returned as an explicit empty array" in prompt
    assert "Do not fabricate a Skill" in prompt
    assert "The same event may support both a persona candidate and a decision candidate" in prompt
    assert "compare against existing knowledge" in prompt
    assert "reuse its stable id" in prompt
    assert "Auxiliary fields cannot replace statement" in prompt
    assert "target_memory_id" in prompt
    assert "new_information" in prompt


def test_grouped_empty_categories_require_explicit_no_durable_signal() -> None:
    payload = {
        "knowledge_candidates": {
            "user_persona": [],
            "decision_rules": [],
            "skills": [],
        },
        "event_dispositions": [
            {
                "event_id": "evt-6",
                "disposition": "no_durable_signal",
                "reason": "No durable signal in this completed event.",
            }
        ],
        "nothing_to_save_reason": "No durable signal in this completed event.",
    }
    completions = SequencedCompletions([[_tool_call("review_batch_result", payload)]])
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_incremental_memory_request())

    assert result.status == "success"
    assert result.actions == ()
    assert result.event_dispositions[0].disposition == "no_durable_signal"


def test_invalid_parameters_json_gets_exactly_one_repair() -> None:
    invalid = _tool_call(
        "review_batch_result",
        {"name": "review_batch_result", "parameters": "{not-json"},
    )
    repaired_payload = {
        "knowledge_candidates": {
            "user_persona": [
                {
                    "content": "User prefers explicit continuation conditions.",
                    "confidence": 0.9,
                    "source_event_ids": ["evt-6"],
                }
            ],
            "decision_rules": [],
            "skills": [],
        },
        "event_dispositions": [
            {"event_id": "evt-6", "disposition": "used", "reason": None}
        ],
        "nothing_to_save_reason": None,
    }
    completions = SequencedCompletions(
        [[invalid], [_tool_call("review_batch_result", repaired_payload)]]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_incremental_memory_request())

    assert result.status == "success"
    assert len(result.actions) == 1
    assert len(completions.calls) == 2
    assert "parameters wrapper contains invalid JSON" in str(
        completions.calls[1]["messages"][1]["content"]
    )


def test_review_trace_summarizes_adapter_conversion_and_routes() -> None:
    grouped = {
        "knowledge_candidates": {
            "user_persona": [
                {
                    "content": "User prefers explicit continuation conditions.",
                    "confidence": 0.9,
                    "source_event_ids": ["evt-knowledge"],
                }
            ],
            "decision_rules": [],
            "skills": [],
        },
        "event_dispositions": [
            {"event_id": "evt-knowledge", "disposition": "used", "reason": None}
        ],
        "nothing_to_save_reason": None,
    }
    wrapped = {
        "name": "review_batch_result",
        "parameters": grouped,
        "provider_metadata": "ignored",
    }
    completions = SequencedCompletions(
        [[_tool_call("review_batch_result", wrapped)]]
    )
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review(
        ReviewRequest(
            event_id="evt-knowledge",
            transcript_text="durable preference",
            final_response="completed",
            allowed_tools=frozenset(
                {"memory_manage", "decision_card_manage", "skill_manage"}
            ),
            current_user_profile="Existing profile.",
            current_decision_rules="Existing rules.",
        )
    )

    assert result.trace is not None
    diagnostics = result.trace["adapter_diagnostics"]
    assert diagnostics["provider_shape"] == "tool_call+name+parameters"
    assert diagnostics["has_arguments"] is True
    assert diagnostics["has_parameters"] is True
    assert diagnostics["candidate_counts"] == {
        "before": 1,
        "after": 1,
        "persona": 1,
        "decision": 0,
        "skill": 0,
    }
    assert diagnostics["routed_action_counts"] == {
        "persona": 1,
        "decision": 0,
        "skill": 0,
    }


def test_second_invalid_batch_output_fails_after_two_total_calls() -> None:
    invalid = _combined_call(
        decision_card_actions=[
            {
                "id": "INVALID ID",
                "title": "title",
                "scenario": "scenario",
                "signals": ["signal"],
                "principle": "principle",
                "outcome": "outcome",
                "boundaries": "boundaries",
                "confidence": 0.8,
                "source_event_ids": ["evt-2"],
            }
        ],
    )
    completions = SequencedCompletions([[invalid], [invalid]])
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "failed"
    assert len(completions.calls) == 2


def test_provider_failure_does_not_trigger_invalid_output_repair() -> None:
    class RateLimitError(RuntimeError):
        status_code = 429

    secret_provider_message = "provider unavailable; api_key=do-not-record"
    completions = SequencedCompletions([RateLimitError(secret_provider_message)])
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "failed"
    assert result.error == "provider request failed (RateLimitError HTTP 429)"
    assert secret_provider_message not in result.summary
    assert secret_provider_message not in result.error
    assert len(completions.calls) == 1


def test_provider_value_error_is_not_misclassified_as_invalid_output() -> None:
    completions = SequencedCompletions([ValueError("invalid provider request")])
    backend = OpenAIReviewBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
        structured_mode="tools",
    )

    result = backend.review_batch(_batch_request())

    assert result.status == "failed"
    assert len(completions.calls) == 1
