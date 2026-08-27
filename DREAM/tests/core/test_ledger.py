from pathlib import Path

import pytest

from dream.core.events import TaskCompletedEvent
from dream.core.ledger import EventLedger
from dream.core.scope import ScopeIds


def _event(event_id: str = "evt-1") -> TaskCompletedEvent:
    return TaskCompletedEvent(
        event_id=event_id,
        task_id="task-1",
        scope=ScopeIds("acme", "assistant", "alice"),
        completed_at="2026-07-15T10:00:00+08:00",
        interrupted=False,
        tool_iterations=12,
        transcript=({"role": "user", "content": "Prefer concise answers"},),
        final_response="Understood.",
        source_refs=(),
    )


def test_event_ledger_is_append_only_and_reloadable(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "ledger" / "events.jsonl")
    event = _event()
    ledger.append(event)
    assert EventLedger(ledger.path).read_all() == [event]


def test_event_ledger_rejects_duplicate_event_ids(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "ledger" / "events.jsonl")
    ledger.append(_event())
    with pytest.raises(ValueError, match="duplicate event_id"):
        ledger.append(_event())


def test_event_ledger_reports_existing_event_id(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "events.jsonl")
    ledger.append(_event("evt-1"))
    assert ledger.contains("evt-1") is True
    assert ledger.contains("evt-2") is False
