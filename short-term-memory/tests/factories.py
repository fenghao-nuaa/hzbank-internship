from datetime import datetime, timezone
from hashlib import sha256

from short_term_memory.models import (
    AutoCompactTrackingState,
    MemoryEvent,
    MemorySummaryEnvelope,
)
from short_term_memory.service.schemas import MemoryReadRequest, MemoryWriteRequest


def memory_event(*, sequence=1, event_id="event-1", content="original", created_at=None):
    timestamp = created_at or datetime(2026, 8, 6, tzinfo=timezone.utc)
    return MemoryEvent(
        sequence=sequence,
        event_id=event_id,
        role="user",
        content_type="conversation",
        content=content,
        metadata={},
        sha256=sha256(content.encode("utf-8")).hexdigest(),
        created_at=timestamp.isoformat(),
    )


def envelope(*, version=1, through=0, generations=None):
    return MemorySummaryEnvelope(
        schema_version=2,
        version=version,
        compressed_through_sequence=through,
        compression_generations=list(generations or []),
        auto_compact_tracking=AutoCompactTrackingState(),
        updated_at="2026-08-06T00:00:00+00:00",
    )


def write_request(event_id="event-1", content="original"):
    return MemoryWriteRequest.model_validate(
        {
            "user_id": "u",
            "session_id": "s",
            "session_seconds": 0,
            "events": [
                {
                    "event_id": event_id,
                    "role": "user",
                    "content_type": "conversation",
                    "content": content,
                    "metadata": {},
                }
            ],
        }
    )


def read_request():
    return MemoryReadRequest(
        user_id="u",
        session_id="s",
        history_turns=10,
        include_effective_config=True,
    )


def write_payload(content="original"):
    return write_request(content=content).model_dump(mode="json")


def read_payload():
    return read_request().model_dump(mode="json")


def scope_headers(label="s"):
    return {
        "x-headroom-user-id": f"u-{label}",
        "x-headroom-session-id": f"s-{label}",
        "x-headroom-project-id": f"p-{label}",
    }
