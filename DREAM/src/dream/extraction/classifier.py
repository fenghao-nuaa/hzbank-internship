"""Artifact-to-manager routing shared by all review backends."""

from dream.extraction.models import ArtifactKind


TOOL_FOR_KIND: dict[ArtifactKind, str | None] = {
    ArtifactKind.DECISION_CARD: "decision_card_manage",
    ArtifactKind.USER_PROFILE: "memory_manage",
    ArtifactKind.USER_TODO: "todo_manage",
    ArtifactKind.AGENT_MEMORY: "memory_manage",
    ArtifactKind.SKILL: "skill_manage",
    ArtifactKind.WIKI_INGEST: None,
    ArtifactKind.NOTHING: None,
}
