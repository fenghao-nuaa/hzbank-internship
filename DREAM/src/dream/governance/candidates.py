"""Persistent observation candidates awaiting repeated dream evidence."""

import hashlib
import json
from pathlib import Path

from dream.memory.artifacts import AtomicArtifactStore
from dream.governance.policy import GovernanceArtifact
from dream.core.scope import ScopePaths


class GovernanceCandidateStore:
    def __init__(self, paths: ScopePaths) -> None:
        self.paths = paths
        self.artifacts = AtomicArtifactStore(paths.agent_root)
        self.relative = (
            Path("governance") / "users" / paths.user_root.name / "candidates.json"
        )

    @staticmethod
    def key_for(artifact: GovernanceArtifact) -> str:
        canonical = (
            f"{artifact.artifact_type.value}\n{artifact.content.strip().casefold()}"
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def load(self) -> tuple[GovernanceArtifact, ...]:
        raw = self.artifacts.read_text(self.relative)
        if not raw:
            return ()
        payload = json.loads(raw)
        values = payload.get("candidates", [])
        if not isinstance(values, list):
            raise ValueError("governance candidates must be a list")
        return tuple(
            GovernanceArtifact.from_dict(value)
            for value in values
            if isinstance(value, dict)
        )

    def reinforce(
        self,
        artifacts: tuple[GovernanceArtifact, ...],
    ) -> tuple[GovernanceArtifact, ...]:
        existing = {self.key_for(value): value for value in self.load()}
        reinforced: list[GovernanceArtifact] = []
        for artifact in artifacts:
            previous = existing.get(self.key_for(artifact))
            if previous is None:
                reinforced.append(artifact)
                continue
            confidence = 1 - ((1 - previous.confidence) * (1 - artifact.confidence))
            reinforced.append(
                GovernanceArtifact(
                    artifact_type=artifact.artifact_type,
                    source_event_ids=tuple(
                        dict.fromkeys(
                            previous.source_event_ids + artifact.source_event_ids
                        )
                    ),
                    confidence=min(confidence, 1.0),
                    content=artifact.content,
                    attributes=artifact.attributes,
                    declared_risk=artifact.declared_risk or previous.declared_risk,
                )
            )
        return tuple(reinforced)

    def record(self, artifacts: tuple[GovernanceArtifact, ...]) -> None:
        values = {self.key_for(value): value for value in self.load()}
        values.update((self.key_for(value), value) for value in artifacts)
        self._write(tuple(values[key] for key in sorted(values)))

    def remove(self, artifacts: tuple[GovernanceArtifact, ...]) -> None:
        removed = {self.key_for(value) for value in artifacts}
        remaining = tuple(
            value for value in self.load() if self.key_for(value) not in removed
        )
        self._write(remaining)

    def _write(self, artifacts: tuple[GovernanceArtifact, ...]) -> None:
        payload = {"candidates": [value.to_dict() for value in artifacts]}
        self.artifacts.write_text(
            self.relative,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
