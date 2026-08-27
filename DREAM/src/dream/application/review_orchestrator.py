"""Best-effort, user-scoped batch Background Review orchestration."""

from dream.core.events import TaskCompletedEvent
from dream.extraction.backend import ReviewBackend
from dream.extraction.models import (
    ReviewBatchEvent,
    ReviewBatchRequest,
    ReviewRequest,
    ReviewResult,
)
from dream.memory.storage.snapshots import ContextSnapshot


class BackgroundReviewOrchestrator:
    def __init__(self, backend: ReviewBackend) -> None:
        self.backend = backend

    def review(
        self,
        event: TaskCompletedEvent,
        *,
        allowed_tools: frozenset[str],
        snapshot: ContextSnapshot | None = None,
    ) -> ReviewResult:
        return self.review_batch(
            (event,),
            allowed_tools_by_event={event.event_id: allowed_tools},
            snapshot=snapshot,
        )

    def review_batch(
        self,
        events: tuple[TaskCompletedEvent, ...],
        *,
        allowed_tools_by_event: dict[str, frozenset[str]],
        snapshot: ContextSnapshot | None = None,
    ) -> ReviewResult:
        reviewable = tuple(
            event for event in events if not event.interrupted and event.final_response
        )
        if not reviewable:
            return ReviewResult(
                actions=(),
                summary="Interrupted or empty tasks were not reviewed.",
                status="skipped",
            )
        scopes = {event.scope for event in reviewable}
        if len(scopes) != 1:
            raise ValueError("one review batch must contain exactly one user scope")
        scope = reviewable[0].scope
        request = ReviewBatchRequest(
            events=tuple(
                ReviewBatchEvent(
                    event_id=event.event_id,
                    transcript_text="\n".join(
                        f"{message.get('role', 'unknown')}: "
                        f"{message.get('content', '')}"
                        for message in event.transcript
                    ),
                    final_response=event.final_response,
                    allowed_tools=allowed_tools_by_event[event.event_id],
                )
                for event in reviewable
            ),
            current_user_profile=(
                snapshot.files[f"users/{scope.user_id}/USER.md"].content
                if snapshot is not None
                else ""
            ),
            current_decision_rules=(
                snapshot.files["DECISION_RULES.md"].content
                if snapshot is not None
                else ""
            ),
            current_decision_cards=(
                tuple(
                    snapshot_file.content
                    for key, snapshot_file in sorted(snapshot.files.items())
                    if key.startswith("decision-cards/") and key.endswith(".md")
                )
                if snapshot is not None
                else ()
            ),
        )
        try:
            batch_method = getattr(self.backend, "review_batch", None)
            if callable(batch_method):
                result = batch_method(request)
            else:
                result = self._legacy_batch(request)
        except Exception as exc:
            return ReviewResult(
                actions=(),
                summary="Background review failed.",
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        invalid = self._invalid_action(result, request)
        if invalid is not None:
            return ReviewResult(
                actions=(),
                summary="Background review returned an invalid scoped action.",
                status="failed",
                error=invalid,
            )
        return result

    def _legacy_batch(self, request: ReviewBatchRequest) -> ReviewResult:
        actions = []
        errors: list[str] = []
        summaries: list[str] = []
        for event in request.events:
            result = self.backend.review(
                ReviewRequest(
                    event_id=event.event_id,
                    transcript_text=event.transcript_text,
                    final_response=event.final_response,
                    allowed_tools=event.allowed_tools,
                    current_user_profile=request.current_user_profile,
                    current_decision_rules=request.current_decision_rules,
                    current_decision_cards=request.current_decision_cards,
                )
            )
            actions.extend(
                action
                for action in result.actions
                if action.tool_name in event.allowed_tools
            )
            summaries.append(result.summary)
            if result.error:
                errors.append(result.error)
            if result.status == "failed":
                return ReviewResult(
                    actions=(),
                    summary="; ".join(summaries),
                    status="failed",
                    error="; ".join(errors) or "legacy batch review failed",
                )
        return ReviewResult(
            actions=tuple(actions),
            summary="; ".join(summaries),
            status="partial" if errors else "success",
            error="; ".join(errors) or None,
        )

    @staticmethod
    def _invalid_action(
        result: ReviewResult, request: ReviewBatchRequest
    ) -> str | None:
        allowed_by_event = {
            event.event_id: event.allowed_tools for event in request.events
        }
        for action in result.actions:
            for event_id in action.evidence_event_ids:
                if event_id not in allowed_by_event:
                    return "action evidence references an event outside the batch"
                if action.tool_name not in allowed_by_event[event_id]:
                    return "action tool is not allowed for its evidence event"
        return None
