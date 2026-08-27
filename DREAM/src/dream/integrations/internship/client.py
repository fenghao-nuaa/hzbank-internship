"""Read completed conversation rounds from an Internship NDJSON API."""

from datetime import datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from dream.config import InternshipSourceSettings


class InternshipMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["user", "assistant", "system", "tool"]
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message content must not be blank")
        return value


class InternshipRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cursor: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    messages: list[InternshipMessage] = Field(min_length=1)
    final_response: str = Field(min_length=1)

    @field_validator(
        "cursor",
        "event_id",
        "user_id",
        "session_id",
        "round_id",
        "final_response",
    )
    @classmethod
    def text_fields_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source text field must not be blank")
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


class SourceFetchError(RuntimeError):
    """Safe source error that excludes credentials and response bodies."""


def parse_ndjson(text: str) -> tuple[InternshipRecord, ...]:
    records: list[InternshipRecord] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(InternshipRecord.model_validate_json(line))
        except (ValueError, ValidationError) as exc:
            raise SourceFetchError(
                f"invalid Internship NDJSON record at line {line_number}"
            ) from exc
    return tuple(records)


class InternshipSourceClient:
    def __init__(
        self,
        settings: InternshipSourceSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    def fetch(self, after: str, limit: int) -> tuple[InternshipRecord, ...]:
        headers = {"Accept": "application/x-ndjson"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        try:
            with httpx.Client(
                timeout=self.settings.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = client.get(
                    self.settings.url,
                    params={"after": after, "limit": limit},
                    headers=headers,
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceFetchError("Internship source request failed") from exc
        return parse_ndjson(response.text)
