"""Typed immutable events consumed by DREAM."""

from dataclasses import dataclass

from dream.core.scope import ScopeIds


@dataclass(frozen=True)
class TaskCompletedEvent:
    event_id: str
    task_id: str
    scope: ScopeIds
    completed_at: str
    interrupted: bool
    tool_iterations: int
    transcript: tuple[dict[str, object], ...]
    final_response: str
    source_refs: tuple[dict[str, object], ...]
