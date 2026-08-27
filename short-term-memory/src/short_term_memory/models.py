"""Shared short-term session and compression data models."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class HeadroomCompressionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


class HeadroomFailureReason(str, Enum):
    SERVICE_UNAVAILABLE = "service_unavailable"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    INVALID_RESPONSE = "invalid_response"
    UNEXPECTED_ERROR = "unexpected_error"


class JournalRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class MemoryContentType(str, Enum):
    CONVERSATION = "conversation"
    CODE = "code"
    DOCUMENT = "document"
    SKILL = "skill"


class FrozenMetadata(dict[str, str]):
    """A serializable mapping that rejects post-validation mutation."""

    def __setitem__(self, key: str, value: str) -> None:
        raise TypeError("metadata is immutable")

    def __delitem__(self, key: str) -> None:
        raise TypeError("metadata is immutable")

    def clear(self) -> None:
        raise TypeError("metadata is immutable")

    def pop(self, key: str, default: str | None = None) -> str:
        raise TypeError("metadata is immutable")

    def popitem(self) -> tuple[str, str]:
        raise TypeError("metadata is immutable")

    def setdefault(self, key: str, default: str | None = None) -> str:
        raise TypeError("metadata is immutable")

    def update(self, *args: object, **kwargs: str) -> None:
        raise TypeError("metadata is immutable")

    def __ior__(self, other: object) -> "FrozenMetadata":
        raise TypeError("metadata is immutable")

    def __copy__(self) -> "FrozenMetadata":
        return type(self)(self)

    def __deepcopy__(self, memo: dict[int, object]) -> "FrozenMetadata":
        copied = type(self)(self)
        memo[id(self)] = copied
        return copied


class FrozenOpaqueMapping(dict[object, Any]):
    """A serializable frozen mapping for opaque Headroom message values."""

    def __setitem__(self, key: object, value: Any) -> None:
        raise TypeError("opaque content is immutable")

    def __delitem__(self, key: object) -> None:
        raise TypeError("opaque content is immutable")

    def clear(self) -> None:
        raise TypeError("opaque content is immutable")

    def pop(self, key: object, default: Any = None) -> Any:
        raise TypeError("opaque content is immutable")

    def popitem(self) -> tuple[object, Any]:
        raise TypeError("opaque content is immutable")

    def setdefault(self, key: object, default: Any = None) -> Any:
        raise TypeError("opaque content is immutable")

    def update(self, *args: object, **kwargs: Any) -> None:
        raise TypeError("opaque content is immutable")

    def __ior__(self, other: object) -> "FrozenOpaqueMapping":
        raise TypeError("opaque content is immutable")

    def __copy__(self) -> "FrozenOpaqueMapping":
        return type(self)(self)

    def __deepcopy__(self, memo: dict[int, object]) -> "FrozenOpaqueMapping":
        copied = type(self)({key: _freeze_opaque(value) for key, value in self.items()})
        memo[id(self)] = copied
        return copied


def _freeze_opaque(value: Any) -> Any:
    """Take an immutable snapshot without interpreting Headroom protocol data."""

    if isinstance(value, dict):
        return FrozenOpaqueMapping(
            {key: _freeze_opaque(nested) for key, nested in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_opaque(nested) for nested in value)
    return value


@dataclass(frozen=True)
class HeadroomCompressionResult:
    status: HeadroomCompressionStatus
    messages: tuple[dict[str, Any], ...]
    fallback_used: bool
    compression_applied: bool = False
    transforms_applied: tuple[str, ...] = ()
    tokens_before: int | None = None
    tokens_after: int | None = None
    tokens_saved: int | None = None
    failure_reason: HeadroomFailureReason | None = None


class SessionCompressionMessage(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    role: str = Field(min_length=1)
    content: Any = None

    @model_validator(mode="before")
    @classmethod
    def freeze_opaque_values(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        return {key: _freeze_opaque(value) for key, value in values.items()}

    @model_validator(mode="after")
    def freeze_extra_fields(self) -> "SessionCompressionMessage":
        if self.__pydantic_extra__ is not None:
            object.__setattr__(
                self,
                "__pydantic_extra__",
                FrozenOpaqueMapping(self.__pydantic_extra__),
            )
        return self


class MemoryEvent(BaseModel):
    """An immutable original event persisted by the memory service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    event_id: str = Field(min_length=1, max_length=200)
    role: JournalRole
    content_type: MemoryContentType
    content: str = Field(min_length=1)
    metadata: dict[str, str] = Field(default_factory=dict)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str = Field(min_length=1)

    @field_validator("metadata")
    @classmethod
    def freeze_metadata(cls, value: dict[str, str]) -> FrozenMetadata:
        return FrozenMetadata(value)


class EventReservation(BaseModel):
    """The sequence and state assigned by an atomic idempotency reservation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    state: Literal["reserved", "pending", "committed"]


class CompressionGeneration(BaseModel):
    """An opaque Headroom compression result over one original-event range."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generation: int = Field(ge=1)
    from_sequence: int = Field(ge=1)
    through_sequence: int = Field(ge=1)
    messages: tuple[SessionCompressionMessage, ...]
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)
    created_at: str = Field(min_length=1)
    ccr_expires_at: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_range(self) -> "CompressionGeneration":
        if self.through_sequence < self.from_sequence:
            raise ValueError("through_sequence must be >= from_sequence")
        return self


class CompactBoundary(BaseModel):
    """Claude compact boundary metadata translated to Journal sequences."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    boundary_id: str = Field(min_length=1)
    trigger: Literal["auto", "manual", "reactive"]
    strategy: Literal["session_memory", "traditional"]
    covered_through_sequence: int = Field(ge=0)
    pre_compact_tokens: int = Field(ge=0)
    true_post_compact_tokens: int = Field(ge=0)
    created_at: str = Field(min_length=1)


class SessionMemoryRevision(BaseModel):
    """Latest Claude-style ten-section Session Memory revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    content: str = Field(min_length=1)
    covered_through_sequence: int = Field(ge=1)
    token_count: int = Field(ge=0)
    extraction_started_at: str | None = None
    updated_at: str = Field(min_length=1)


class ContextRevision(BaseModel):
    """The single active post-compact context revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    boundary: CompactBoundary
    summary_message: SessionCompressionMessage
    messages_to_keep: tuple[SessionCompressionMessage, ...] = Field(
        default_factory=tuple
    )
    covered_generation_ids: tuple[int, ...] = Field(default_factory=tuple)
    updated_at: str = Field(min_length=1)

    @field_validator("covered_generation_ids")
    @classmethod
    def validate_generation_ids(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(value < 1 for value in values):
            raise ValueError("covered generation IDs must be positive")
        return tuple(values)


class AutoCompactTrackingState(BaseModel):
    """Claude auto-compact same-chain and circuit-breaker state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    compacted: bool = False
    turn_counter: int = Field(default=0, ge=0)
    turn_id: str = ""
    consecutive_failures: int = Field(default=0, ge=0)

    def record_failure(self) -> "AutoCompactTrackingState":
        return self.model_copy(
            update={
                "compacted": False,
                "consecutive_failures": self.consecutive_failures + 1,
            }
        )

    def reset_success(self, turn_id: str) -> "AutoCompactTrackingState":
        if not turn_id:
            raise ValueError("turn_id must not be blank")
        return AutoCompactTrackingState(
            compacted=True,
            turn_counter=0,
            turn_id=turn_id,
            consecutive_failures=0,
        )


class MemorySummaryEnvelope(BaseModel):
    """Redis envelope for Headroom assets and Claude activity compaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    version: int = Field(ge=1)
    compressed_through_sequence: int = Field(ge=0)
    compression_generations: tuple[CompressionGeneration, ...] = Field(
        default_factory=tuple
    )
    session_memory: SessionMemoryRevision | None = None
    active_revision: ContextRevision | None = None
    auto_compact_tracking: AutoCompactTrackingState = Field(
        default_factory=AutoCompactTrackingState
    )
    updated_at: str = Field(min_length=1)


def migrate_v1_envelope(raw: dict[str, Any]) -> MemorySummaryEnvelope:
    """Lazily discard the retired five-category fields at the Redis boundary."""

    if raw.get("schema_version") == 2:
        return MemorySummaryEnvelope.model_validate(raw)
    allowed = {
        "version",
        "compressed_through_sequence",
        "compression_generations",
        "updated_at",
    }
    return MemorySummaryEnvelope.model_validate(
        {
            "schema_version": 2,
            **{key: value for key, value in raw.items() if key in allowed},
            "session_memory": None,
            "active_revision": None,
            "auto_compact_tracking": {},
        }
    )


@dataclass(frozen=True)
class PreparedTurn:
    user_id: str
    session_id: str
    history: tuple[dict[str, Any], ...]
    timestamp: datetime | None
    session_seconds: int
    headroom_headers: dict[str, str]
    headroom_proxy_url: str | None


@dataclass(frozen=True)
class CompletionResult:
    headroom_queued: bool
