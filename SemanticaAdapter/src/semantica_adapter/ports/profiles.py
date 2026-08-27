"""Versioned Agent profile repository contract."""

from typing import Protocol

from semantica_adapter.domain.models import AgentProfile


class AgentProfileRepository(Protocol):
    def save(self, profile: AgentProfile) -> None: ...

    def get(self, agent_id: str, profile_version: str | None = None) -> AgentProfile: ...
