import json

import pytest

from dream.extraction.models import ArtifactKind, ReviewRequest
from dream.validation.recorded_review import (
    RecordedReviewBackend,
    RecordedReviewError,
)


def request(*allowed: str) -> ReviewRequest:
    return ReviewRequest(
        event_id="evt-001",
        transcript_text="conversation",
        final_response="answer",
        allowed_tools=frozenset(allowed),
    )


def document() -> str:
    return json.dumps(
        {
            "reviews": [
                {
                    "event_id": "evt-001",
                    "actions": [
                        {
                            "tool_name": "memory_manage",
                            "payload": {
                                "action": "add",
                                "content": "Prefers step-by-step explanations.",
                            },
                        },
                        {
                            "tool_name": "decision_card_manage",
                            "payload": {
                                "id": "verify-before-retry",
                                "title": "重试前核验",
                                "scenario": "交易状态不明确",
                                "signals": ["状态未知"],
                                "principle": "先核验，再重试。",
                                "outcome": "避免重复付款。",
                                "boundaries": "明确失败时才可重试。",
                                "confidence": 0.9,
                            },
                        },
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )


def test_recorded_review_replays_strict_actions_with_event_evidence() -> None:
    backend = RecordedReviewBackend.from_json_documents((document(),))

    result = backend.review(request("memory_manage", "decision_card_manage"))

    assert [action.kind for action in result.actions] == [
        ArtifactKind.USER_PROFILE,
        ArtifactKind.DECISION_CARD,
    ]
    assert all(action.source_event_id == "evt-001" for action in result.actions)


def test_recorded_review_respects_allowed_tools() -> None:
    backend = RecordedReviewBackend.from_json_documents((document(),))

    result = backend.review(request("memory_manage"))

    assert len(result.actions) == 1
    assert result.actions[0].tool_name == "memory_manage"


def test_recorded_review_supports_evidence_backed_preference_replacement() -> None:
    payload = json.loads(document())
    memory = payload["reviews"][0]["actions"][0]["payload"]
    memory.update(
        {
            "action": "replace",
            "old_content": "Prefers long explanations.",
            "content": "Prefers short checklists with risk details only.",
        }
    )

    backend = RecordedReviewBackend.from_json_documents((json.dumps(payload),))
    result = backend.review(request("memory_manage"))

    assert result.actions[0].payload["action"] == "replace"
    assert result.actions[0].payload["old_content"] == "Prefers long explanations."


def test_recorded_review_rejects_duplicate_events() -> None:
    with pytest.raises(RecordedReviewError, match="duplicate event"):
        RecordedReviewBackend.from_json_documents((document(), document()))


def test_recorded_review_rejects_unknown_tools_and_extra_fields() -> None:
    payload = json.loads(document())
    payload["reviews"][0]["actions"][0]["tool_name"] = "shell"
    with pytest.raises(RecordedReviewError, match="invalid"):
        RecordedReviewBackend.from_json_documents((json.dumps(payload),))

    payload = json.loads(document())
    payload["secret"] = "not allowed"
    with pytest.raises(RecordedReviewError, match="invalid"):
        RecordedReviewBackend.from_json_documents((json.dumps(payload),))
