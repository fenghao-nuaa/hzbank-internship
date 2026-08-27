"""Strict JSONL input for completed validation tasks."""

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from dream.core.events import TaskCompletedEvent
from dream.core.scope import ScopeIds


class ManualMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant", "system", "tool"]
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message content must not be blank")
        return value


class ManualConversationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    source: Literal["codex-thread", "manual-import"] = "manual-import"
    session_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    messages: list[ManualMessage] = Field(min_length=2)
    final_response: str = Field(min_length=1)

    @field_validator(
        "event_id",
        "tenant_id",
        "agent_id",
        "user_id",
        "session_id",
        "task_id",
        "final_response",
    )
    @classmethod
    def text_fields_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text field must not be blank")
        return value

    @field_validator("completed_at")
    @classmethod
    def completed_at_must_include_timezone(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("completed_at must be ISO 8601") from exc
        if parsed.tzinfo is None:
            raise ValueError("completed_at must include a timezone")
        return value

    @model_validator(mode="after")
    def completed_task_is_consistent(self) -> "ManualConversationRecord":
        assistants = [
            message.content for message in self.messages if message.role == "assistant"
        ]
        if not assistants:
            raise ValueError("completed task requires an assistant message")
        if not any(message.role == "user" for message in self.messages):
            raise ValueError("completed task requires a user message")
        if assistants[-1] != self.final_response:
            raise ValueError("final_response must equal the last assistant message")
        return self


class ManualSourceError(ValueError):
    """Safe validation error that never includes the input record."""


def parse_manual_ndjson(text: str) -> tuple[ManualConversationRecord, ...]:
    records: list[ManualConversationRecord] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(ManualConversationRecord.model_validate_json(line))
        except ValidationError as exc:
            first_error = exc.errors(include_input=False)[0]
            reason = str(first_error.get("msg", "record validation failed"))
            raise ManualSourceError(
                f"invalid manual JSONL record at line {line_number}: {reason}"
            ) from exc
    return tuple(records)


def manual_record_to_event(record: ManualConversationRecord) -> TaskCompletedEvent:
    return TaskCompletedEvent(
        event_id=record.event_id,
        task_id=record.task_id,
        scope=ScopeIds(record.tenant_id, record.agent_id, record.user_id),
        completed_at=record.completed_at,
        interrupted=False,
        tool_iterations=10,
        transcript=tuple(message.model_dump() for message in record.messages),
        final_response=record.final_response,
        source_refs=(
            {
                "source": record.source,
                "session_id": record.session_id,
            },
        ),
    )
