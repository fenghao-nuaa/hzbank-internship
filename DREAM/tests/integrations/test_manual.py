import json

import pytest

from dream.core.scope import ScopeIds
from dream.integrations.manual import (
    ManualSourceError,
    manual_record_to_event,
    parse_manual_ndjson,
)


def valid_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "event_id": "evt_project_manager_001",
        "tenant_id": "dream-lab",
        "agent_id": "enterprise-colleague",
        "user_id": "project-manager",
        "source": "codex-thread",
        "session_id": "session-001",
        "task_id": "task-001",
        "completed_at": "2026-07-17T10:00:00+08:00",
        "messages": [
            {"role": "user", "content": "先告诉我结论。"},
            {"role": "assistant", "content": "结论：接口尚待联调。"},
        ],
        "final_response": "结论：接口尚待联调。",
    }
    record.update(overrides)
    return record


def valid_line(**overrides: object) -> str:
    return json.dumps(valid_record(**overrides), ensure_ascii=False)


def test_manual_record_accepts_one_complete_task() -> None:
    records = parse_manual_ndjson(valid_line())

    assert len(records) == 1
    assert records[0].task_id == "task-001"
    assert records[0].messages[-1].role == "assistant"


def test_manual_record_maps_to_scoped_completed_event() -> None:
    event = manual_record_to_event(parse_manual_ndjson(valid_line())[0])

    assert event.event_id == "evt_project_manager_001"
    assert event.task_id == "task-001"
    assert event.scope == ScopeIds(
        "dream-lab", "enterprise-colleague", "project-manager"
    )
    assert event.source_refs == (
        {"source": "codex-thread", "session_id": "session-001"},
    )


def test_manual_record_rejects_unknown_source() -> None:
    with pytest.raises(ManualSourceError, match="line 1"):
        parse_manual_ndjson(valid_line(source="unverified-chat"))


def test_manual_record_rejects_mismatched_final_response() -> None:
    with pytest.raises(ManualSourceError, match="last assistant"):
        parse_manual_ndjson(valid_line(final_response="different"))


def test_manual_record_rejects_hidden_persona_fields() -> None:
    with pytest.raises(ManualSourceError, match="line 1"):
        parse_manual_ndjson(valid_line(hidden_persona={"role": "manager"}))


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "assistant", "content": "没有用户消息"}],
        [{"role": "user", "content": "没有 AI 回复"}],
    ],
)
def test_manual_record_requires_user_and_assistant_messages(
    messages: list[dict[str, str]],
) -> None:
    with pytest.raises(ManualSourceError, match="line 1"):
        parse_manual_ndjson(valid_line(messages=messages))


def test_manual_record_rejects_timestamp_without_timezone() -> None:
    with pytest.raises(ManualSourceError, match="line 1"):
        parse_manual_ndjson(valid_line(completed_at="2026-07-17T10:00:00"))
