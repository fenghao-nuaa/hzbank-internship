from pathlib import Path

import pytest

from dream.core.events import TaskCompletedEvent
from dream.memory.publication import (
    PublicationStatus,
    PublicationStore,
    PublicationTransitionError,
)
from dream.core.scope import ScopeIds, resolve_scope
from dream.application.service import DreamService


def store(tmp_path: Path, user: str = "python-beginner") -> PublicationStore:
    paths = resolve_scope(
        tmp_path,
        ScopeIds("dream-lab", "enterprise-colleague", user),
    )
    return PublicationStore(paths)


def activate_v1(publications: PublicationStore):
    version = publications.begin(("evt-1",), "evt-1", "before-v1")
    version = publications.mark_dreaming(version.version)
    version = publications.mark_ready_for_review(
        version.version,
        "after-v1",
        "character-sha-v1",
        "user-sha-v1",
    )
    version = publications.approve(version.version)
    version = publications.confirm_writeback(
        version.version,
        character_written=True,
        user_written=True,
    )
    return publications.activate(version.version)


def test_publication_requires_both_manual_writebacks_before_activation(
    tmp_path: Path,
) -> None:
    publications = store(tmp_path)
    version = publications.begin(("evt-1",), "evt-1", "before-snapshot")
    version = publications.mark_dreaming(version.version)
    version = publications.mark_ready_for_review(
        version.version,
        "after-snapshot",
        "character-sha",
        "user-sha",
    )
    version = publications.approve(version.version)
    version = publications.confirm_writeback(
        version.version,
        character_written=True,
        user_written=False,
    )

    with pytest.raises(PublicationTransitionError, match="both writebacks"):
        publications.activate(version.version)


def test_failed_candidate_keeps_previous_active_version(tmp_path: Path) -> None:
    publications = store(tmp_path)
    active = activate_v1(publications)
    publications.note_completed_event("evt-2")
    candidate = publications.begin(("evt-2",), "evt-2", "before-v2")
    candidate = publications.mark_dreaming(candidate.version)

    failed = publications.fail(candidate.version, "curator failed")

    assert active.version == 1
    assert publications.active().version == 1
    assert failed.status is PublicationStatus.FAILED
    assert failed.fallback_version == 1
    assert publications.pending_event_ids() == ("evt-2",)


def test_activation_clears_only_the_events_in_that_version(tmp_path: Path) -> None:
    publications = store(tmp_path)
    publications.note_completed_event("evt-1")
    publications.note_completed_event("evt-2")
    publications.note_completed_event("evt-3")
    version = publications.begin(("evt-1", "evt-2"), "evt-2", "before")
    version = publications.mark_dreaming(version.version)
    version = publications.mark_ready_for_review(
        version.version, "after", "char-sha", "user-sha"
    )
    version = publications.approve(version.version)
    publications.confirm_writeback(
        version.version, character_written=True, user_written=True
    )

    publications.activate(version.version)

    assert publications.pending_event_ids() == ("evt-3",)
    assert publications.active().processed_through_event_id == "evt-2"


def test_illegal_transition_is_rejected(tmp_path: Path) -> None:
    publications = store(tmp_path)
    version = publications.begin(("evt-1",), "evt-1", "before")

    with pytest.raises(PublicationTransitionError):
        publications.approve(version.version)


def test_governed_candidate_can_move_directly_to_ready_for_activation(
    tmp_path: Path,
) -> None:
    publications = store(tmp_path)
    version = publications.begin(("evt-auto",), "evt-auto", "before")
    version = publications.mark_dreaming(version.version)

    ready = publications.mark_ready_for_activation(
        version.version,
        "after",
        "character-sha",
        "user-sha",
    )

    assert ready.status is PublicationStatus.READY_FOR_ACTIVATION
    assert ready.character_definition_written is True
    assert ready.user_persona_written is True
    assert publications.activate(ready.version).status is PublicationStatus.ACTIVE


def test_ingested_conversation_is_noted_as_pending(tmp_path: Path) -> None:
    ids = ScopeIds("dream-lab", "enterprise-colleague", "project-manager")
    service = DreamService(tmp_path)
    service.ingest_conversation(
        TaskCompletedEvent(
            event_id="evt-project-1",
            task_id="task-1",
            scope=ids,
            completed_at="2026-07-17T10:00:00+08:00",
            interrupted=False,
            tool_iterations=10,
            transcript=(
                {"role": "user", "content": "结论优先。"},
                {"role": "assistant", "content": "明白。"},
            ),
            final_response="明白。",
            source_refs=(),
        )
    )

    assert PublicationStore(resolve_scope(tmp_path, ids)).pending_event_ids() == (
        "evt-project-1",
    )
