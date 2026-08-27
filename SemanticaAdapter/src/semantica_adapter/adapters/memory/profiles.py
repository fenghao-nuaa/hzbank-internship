"""In-memory immutable Agent profile versions."""

from semantica_adapter.domain.models import AgentProfile


class MemoryAgentProfileRepository:
    def __init__(self) -> None:
        self._profiles: dict[tuple[str, str], AgentProfile] = {}
        self._versions: dict[str, list[str]] = {}

    def save(self, profile: AgentProfile) -> None:
        key = (profile.agent_id, profile.profile_version)
        if key in self._profiles:
            raise ValueError(f"Agent profile version already exists: {key}")
        self._profiles[key] = profile
        self._versions.setdefault(profile.agent_id, []).append(profile.profile_version)

    def get(self, agent_id: str, profile_version: str | None = None) -> AgentProfile:
        versions = self._versions.get(agent_id)
        if not versions:
            raise KeyError(f"unknown Agent profile: {agent_id}")
        selected = profile_version or versions[-1]
        try:
            return self._profiles[(agent_id, selected)]
        except KeyError as error:
            raise KeyError(f"unknown Agent profile version: {agent_id}@{selected}") from error
