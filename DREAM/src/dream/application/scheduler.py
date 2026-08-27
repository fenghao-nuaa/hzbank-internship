"""Adaptive, user-scoped scheduling for pending conversation reviews."""

from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from dream.core.events import TaskCompletedEvent
from dream.core.scope import ScopeIds


TokenEstimator = Callable[[TaskCompletedEvent], int]


@dataclass(frozen=True)
class ReviewSchedulePolicy:
    """Thresholds that make one user-scoped pending queue eligible for review."""

    idle_after: timedelta = timedelta(hours=2)
    max_batch_tokens: int = 16_000
    max_batch_events: int = 20
    max_wait: timedelta = timedelta(hours=24)

    def __post_init__(self) -> None:
        if self.idle_after <= timedelta(0):
            raise ValueError("idle_after must be positive")
        if self.max_batch_tokens < 1:
            raise ValueError("max_batch_tokens must be positive")
        if self.max_batch_events < 1:
            raise ValueError("max_batch_events must be positive")
        if self.max_wait <= timedelta(0):
            raise ValueError("max_wait must be positive")


@dataclass(frozen=True)
class PendingReviewBatch:
    scope: ScopeIds
    events: tuple[TaskCompletedEvent, ...]
    estimated_tokens: int
    trigger_reasons: tuple[str, ...]


def estimate_event_tokens(event: TaskCompletedEvent) -> int:
    """Return a stable local estimate without invoking a tokenizer or service."""

    text_size = len(event.final_response)
    text_size += sum(
        len(str(message.get("content", ""))) for message in event.transcript
    )
    return max(1, (text_size + 3) // 4)


def _event_time(event: TaskCompletedEvent) -> datetime:
    try:
        parsed = datetime.fromisoformat(event.completed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("completed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("completed_at must include a timezone")
    return parsed.astimezone(timezone.utc)


class DreamScheduler:
    def __init__(
        self,
        review_threshold: int = 10,
        *,
        policy: ReviewSchedulePolicy | None = None,
        token_estimator: TokenEstimator = estimate_event_tokens,
    ) -> None:
        # Retained for compatibility with callers that still inspect the legacy
        # Hermes iteration threshold. Adaptive scheduling does not depend on it.
        if review_threshold < 1:
            raise ValueError("review_threshold must be positive")
        self.review_threshold = review_threshold
        self.policy = policy or ReviewSchedulePolicy()
        self.token_estimator = token_estimator
        self._pending: deque[TaskCompletedEvent] = deque()
        self._iterations: dict[ScopeIds, int] = defaultdict(int)

    def enqueue(self, event: TaskCompletedEvent) -> None:
        if event.interrupted or not event.final_response:
            return
        _event_time(event)
        self._pending.append(event)
        self._iterations[event.scope] += max(0, event.tool_iterations)

    def enqueue_unless_pending(self, event: TaskCompletedEvent) -> None:
        if event.event_id in self.pending_event_ids():
            return
        self.enqueue(event)

    def pending_event_ids(self, scope: ScopeIds | None = None) -> tuple[str, ...]:
        return tuple(
            event.event_id
            for event in self._pending
            if scope is None or event.scope == scope
        )

    def pop_pending(self, scope: ScopeIds | None = None) -> TaskCompletedEvent | None:
        if scope is None:
            if not self._pending:
                return None
            return self._pending.popleft()
        for index, event in enumerate(self._pending):
            if event.scope == scope:
                del self._pending[index]
                return event
        return None

    def pop_due_batch(self, now: datetime) -> PendingReviewBatch | None:
        """Remove and return the oldest eligible user-scoped batch."""

        now_utc = self._normalized_now(now)
        grouped: dict[ScopeIds, list[TaskCompletedEvent]] = {}
        for event in self._pending:
            grouped.setdefault(event.scope, []).append(event)

        for scope, events in grouped.items():
            events.sort(key=_event_time)
            reasons = self._trigger_reasons(events, now_utc)
            if not reasons:
                continue
            selected, estimated_tokens = self._select_batch(events)

            selected_ids = {event.event_id for event in selected}
            self._pending = deque(
                event for event in self._pending if event.event_id not in selected_ids
            )
            return PendingReviewBatch(
                scope=scope,
                events=tuple(selected),
                estimated_tokens=estimated_tokens,
                trigger_reasons=reasons,
            )
        return None

    def due_scopes(self, now: datetime) -> tuple[ScopeIds, ...]:
        """Return eligible scopes without consuming their pending events."""

        now_utc = self._normalized_now(now)
        grouped: dict[ScopeIds, list[TaskCompletedEvent]] = {}
        for event in self._pending:
            grouped.setdefault(event.scope, []).append(event)
        return tuple(
            scope
            for scope, events in grouped.items()
            if self._trigger_reasons(sorted(events, key=_event_time), now_utc)
        )

    def split_pending_batches(
        self,
        events: list[TaskCompletedEvent],
        *,
        trigger_reasons: tuple[str, ...] = ("manual",),
    ) -> tuple[PendingReviewBatch, ...]:
        """Split drained events by user scope and configured batch limits."""

        grouped: dict[ScopeIds, list[TaskCompletedEvent]] = {}
        for event in events:
            grouped.setdefault(event.scope, []).append(event)
        batches: list[PendingReviewBatch] = []
        for scope, scoped_events in grouped.items():
            remaining = sorted(scoped_events, key=_event_time)
            while remaining:
                selected, estimated_tokens = self._select_batch(remaining)
                batches.append(
                    PendingReviewBatch(
                        scope=scope,
                        events=tuple(selected),
                        estimated_tokens=estimated_tokens,
                        trigger_reasons=trigger_reasons,
                    )
                )
                del remaining[: len(selected)]
        return tuple(batches)

    def _select_batch(
        self, events: list[TaskCompletedEvent]
    ) -> tuple[list[TaskCompletedEvent], int]:
        selected: list[TaskCompletedEvent] = []
        estimated_tokens = 0
        for event in events:
            event_tokens = max(1, self.token_estimator(event))
            if selected and (
                len(selected) >= self.policy.max_batch_events
                or estimated_tokens + event_tokens > self.policy.max_batch_tokens
            ):
                break
            selected.append(event)
            estimated_tokens += event_tokens
            if len(selected) >= self.policy.max_batch_events:
                break
        return selected, estimated_tokens

    def _trigger_reasons(
        self, events: list[TaskCompletedEvent], now: datetime
    ) -> tuple[str, ...]:
        event_times = [_event_time(event) for event in events]
        total_tokens = sum(max(1, self.token_estimator(event)) for event in events)
        reasons: list[str] = []
        if now - max(event_times) >= self.policy.idle_after:
            reasons.append("idle")
        if total_tokens >= self.policy.max_batch_tokens:
            reasons.append("tokens")
        if len(events) >= self.policy.max_batch_events:
            reasons.append("events")
        if now - min(event_times) >= self.policy.max_wait:
            reasons.append("max_wait")
        return tuple(reasons)

    @staticmethod
    def _normalized_now(now: datetime) -> datetime:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must include a timezone")
        return now.astimezone(timezone.utc)

    def scope_is_ready(self, scope: ScopeIds) -> bool:
        return self._iterations[scope] >= self.review_threshold

    def mark_review_accepted(self, scope: ScopeIds) -> None:
        self._iterations[scope] = 0
