"""Explicit approval authorization used by tests and local examples."""

from semantica_adapter.domain.models import ApprovalRecord, PolicyExceptionRecord


class MemoryApprovalWorkflow:
    def __init__(self, authorized_actors: set[tuple[str, str]] | None = None) -> None:
        self._authorized_actors = set(authorized_actors or set())

    def authorize(self, record: ApprovalRecord | PolicyExceptionRecord) -> bool:
        actor_id = record.approver_id
        role = getattr(record, "approver_role", str(record.metadata.get("approver_role", "")))
        return (actor_id, role) in self._authorized_actors
