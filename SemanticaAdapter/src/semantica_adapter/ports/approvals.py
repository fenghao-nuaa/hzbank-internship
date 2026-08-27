"""Company approval authorization contract."""

from typing import Protocol

from semantica_adapter.domain.models import ApprovalRecord, PolicyExceptionRecord


class ApprovalWorkflowPort(Protocol):
    def authorize(self, record: ApprovalRecord | PolicyExceptionRecord) -> bool: ...
