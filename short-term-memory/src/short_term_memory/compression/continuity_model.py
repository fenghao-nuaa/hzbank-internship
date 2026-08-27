"""Provider boundary shared by Claude-style L3 and L4 compaction."""

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class CompactionModelResponse(BaseModel):
    """Provider-neutral compact response and its reported usage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str = Field(min_length=1)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class PromptTooLongError(RuntimeError):
    """Provider-normalized compact prompt-too-long failure."""

    def __init__(self, token_gap: int | None = None) -> None:
        if token_gap is not None and token_gap < 1:
            raise ValueError("token_gap must be positive")
        super().__init__("compact prompt is too long")
        self.token_gap = token_gap


class ContinuityCompactionModel(Protocol):
    async def update_session_memory(
        self,
        *,
        current_memory: str,
        messages: tuple[dict[str, Any], ...],
        prompt: str,
        model: str,
        query_source: Literal["session_memory"] = "session_memory",
    ) -> str: ...

    async def compact(
        self,
        *,
        messages: tuple[dict[str, Any], ...],
        prompt: str,
        model: str,
        max_output_tokens: int,
        query_source: Literal["compact"] = "compact",
    ) -> CompactionModelResponse: ...
