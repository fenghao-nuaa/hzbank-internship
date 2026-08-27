import json
from pathlib import Path

from dream.config import InternshipSourceSettings
from dream.core.events import TaskCompletedEvent
from dream.core.scope import ScopeIds
from dream.integrations.internship.client import InternshipRecord


VALID_RECORD: dict[str, object] = {
    "cursor": "101",
    "event_id": "evt-101",
    "user_id": "alice",
    "session_id": "session-1",
    "round_id": "round-1",
    "completed_at": "2026-07-15T10:00:00Z",
    "messages": [
        {"role": "user", "content": "Prefer concise answers"},
        {"role": "assistant", "content": "Understood"},
    ],
    "final_response": "Understood",
}


def source_record(**overrides: object) -> InternshipRecord:
    return InternshipRecord.model_validate({**VALID_RECORD, **overrides})


def source_line(**overrides: object) -> str:
    return json.dumps({**VALID_RECORD, **overrides}, ensure_ascii=False)


def source_settings(**overrides: object) -> InternshipSourceSettings:
    values: dict[str, object] = {
        "enabled": True,
        "url": "https://memory.example/v1/memory/dream-export",
        "api_key": "secret",
        "tenant_id": "acme",
        "agent_id": "assistant",
        "batch_size": 100,
        "timeout_seconds": 5.0,
        "interval_seconds": 300,
    }
    return InternshipSourceSettings(**{**values, **overrides})


def write_source_env(
    tmp_path: Path,
    *,
    interval_seconds: int = 300,
) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_INTERNSHIP_SOURCE_ENABLED=true\n"
        "DREAM_INTERNSHIP_SOURCE_URL=https://memory.example/v1/memory/dream-export\n"
        "DREAM_INTERNSHIP_SOURCE_API_KEY=secret\n"
        "DREAM_INTERNSHIP_SOURCE_TENANT_ID=acme\n"
        "DREAM_INTERNSHIP_SOURCE_AGENT_ID=assistant\n"
        f"DREAM_INTERNSHIP_SOURCE_INTERVAL_SECONDS={interval_seconds}\n",
        encoding="utf-8",
    )
    return env_file


def local_event(event_id: str) -> TaskCompletedEvent:
    return TaskCompletedEvent(
        event_id=event_id,
        task_id="local-task",
        scope=ScopeIds("acme", "assistant", "alice"),
        completed_at="2026-07-15T10:00:00Z",
        interrupted=False,
        tool_iterations=10,
        transcript=(
            {"role": "user", "content": "Prefer concise answers"},
            {"role": "assistant", "content": "Understood"},
        ),
        final_response="Understood",
        source_refs=(),
    )
