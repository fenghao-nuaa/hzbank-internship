"""Strict replay adapter for audited validation-model review output."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from dream.extraction.models import (
    ArtifactKind,
    ReviewAction,
    ReviewRequest,
    ReviewResult,
)


class RecordedReviewError(ValueError):
    """Recorded semantic review evidence is malformed or ambiguous."""


class _MemoryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["add", "replace"]
    content: str = Field(min_length=1, max_length=1000)
    old_content: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def replacement_names_existing_content(self) -> "_MemoryPayload":
        if self.action == "replace" and self.old_content is None:
            raise ValueError("replace requires old_content")
        if self.action == "add" and self.old_content is not None:
            raise ValueError("add must not include old_content")
        return self


class _DecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    title: str = Field(min_length=1, max_length=200)
    scenario: str = Field(min_length=1, max_length=1000)
    signals: tuple[str, ...] = Field(min_length=1, max_length=20)
    principle: str = Field(min_length=1, max_length=1000)
    outcome: str = Field(min_length=1, max_length=1000)
    boundaries: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)


class _MemoryAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: Literal["memory_manage"]
    payload: _MemoryPayload


class _DecisionAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: Literal["decision_card_manage"]
    payload: _DecisionPayload


class _EventReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1, max_length=200)
    actions: tuple[_MemoryAction | _DecisionAction, ...] = Field(max_length=10)


class _ReviewDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reviews: tuple[_EventReview, ...] = Field(min_length=1, max_length=100)


class RecordedReviewBackend:
    """Replay human-audited structured reviews through the normal DREAM pipeline."""

    def __init__(self, reviews: dict[str, _EventReview]) -> None:
        self._reviews = dict(reviews)

    @classmethod
    def from_json_documents(
        cls,
        documents: tuple[str, ...],
    ) -> "RecordedReviewBackend":
        reviews: dict[str, _EventReview] = {}
        try:
            parsed = tuple(
                _ReviewDocument.model_validate_json(document)
                for document in documents
            )
        except ValidationError as exc:
            raise RecordedReviewError("recorded review document is invalid") from exc
        for document in parsed:
            for review in document.reviews:
                if review.event_id in reviews:
                    raise RecordedReviewError(
                        f"duplicate event review: {review.event_id}"
                    )
                reviews[review.event_id] = review
        return cls(reviews)

    @classmethod
    def from_paths(cls, paths: tuple[Path, ...]) -> "RecordedReviewBackend":
        try:
            documents = tuple(path.read_text(encoding="utf-8") for path in paths)
        except OSError as exc:
            raise RecordedReviewError("recorded review file cannot be read") from exc
        return cls.from_json_documents(documents)

    def review(self, request: ReviewRequest) -> ReviewResult:
        recorded = self._reviews.get(request.event_id)
        if recorded is None:
            return ReviewResult(actions=(), summary="No audited review was recorded.")
        actions: list[ReviewAction] = []
        for action in recorded.actions:
            if action.tool_name not in request.allowed_tools:
                continue
            if isinstance(action, _MemoryAction):
                kind = ArtifactKind.USER_PROFILE
                payload = action.payload.model_dump(exclude_none=True)
                payload["target"] = "user"
            else:
                kind = ArtifactKind.DECISION_CARD
                payload = action.payload.model_dump()
                payload["signals"] = list(payload["signals"])
            actions.append(
                ReviewAction(
                    kind=kind,
                    tool_name=action.tool_name,
                    payload=payload,
                    source_event_id=request.event_id,
                )
            )
        return ReviewResult(
            actions=tuple(actions),
            summary=f"Replayed {len(actions)} audited review action(s).",
        )
