"""Semantic identifier aliases used by DREAM domain models.

Identifiers remain ordinary strings so the architecture migration does not
change JSON payloads, public APIs, or persisted ledger formats.
"""

from typing import TypeAlias


TenantId: TypeAlias = str
AgentId: TypeAlias = str
UserId: TypeAlias = str
EventId: TypeAlias = str
TaskId: TypeAlias = str

__all__ = ["AgentId", "EventId", "TaskId", "TenantId", "UserId"]
