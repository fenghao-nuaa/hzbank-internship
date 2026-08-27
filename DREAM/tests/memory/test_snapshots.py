from pathlib import Path

import pytest

from dream.memory.artifacts import AtomicArtifactStore
from dream.core.scope import ScopeIds, resolve_scope
from dream.memory.storage.snapshots import SnapshotStore


def test_background_write_does_not_mutate_existing_snapshot(tmp_path: Path) -> None:
    ids = ScopeIds("acme", "assistant", "alice")
    paths = resolve_scope(tmp_path, ids)
    store = AtomicArtifactStore(paths.agent_root)
    store.write_text(Path("users/alice/USER.md"), "prefers detail\n")
    snapshots = SnapshotStore(paths, store)
    first = snapshots.create(ids)

    store.write_text(Path("users/alice/USER.md"), "prefers concise answers\n")
    second = snapshots.create(ids)

    assert first.files["users/alice/USER.md"].content == "prefers detail\n"
    assert second.files["users/alice/USER.md"].content == "prefers concise answers\n"
    assert first.snapshot_id != second.snapshot_id


@pytest.mark.parametrize("path", [Path("/tmp/escape"), Path("../escape")])
def test_artifact_store_rejects_paths_outside_scope(tmp_path: Path, path: Path) -> None:
    store = AtomicArtifactStore(tmp_path / "agent")
    with pytest.raises(ValueError):
        store.write_text(path, "blocked")


def test_context_snapshot_restores_writeback_and_removes_new_decision_card(
    tmp_path: Path,
) -> None:
    ids = ScopeIds("acme", "assistant", "alice")
    paths = resolve_scope(tmp_path, ids)
    store = AtomicArtifactStore(paths.agent_root)
    store.write_text(Path("CHARACTER_DEFINITION.md"), "stable character\n")
    store.write_text(Path("users/alice/USER_PERSONA.md"), "stable persona\n")
    snapshots = SnapshotStore(paths, store)
    snapshot = snapshots.create(ids)

    store.write_text(Path("CHARACTER_DEFINITION.md"), "candidate character\n")
    store.write_text(Path("users/alice/USER_PERSONA.md"), "candidate persona\n")
    store.write_text(Path("decision-cards/new-card.md"), "new candidate card\n")
    snapshots.restore(snapshot.snapshot_id, ids)

    assert store.read_text(Path("CHARACTER_DEFINITION.md")) == "stable character\n"
    assert store.read_text(Path("users/alice/USER_PERSONA.md")) == "stable persona\n"
    assert not store.resolve(Path("decision-cards/new-card.md")).exists()


def test_context_snapshot_restores_archives_curator_state_and_file_existence(
    tmp_path: Path,
) -> None:
    ids = ScopeIds("acme", "assistant", "alice")
    paths = resolve_scope(tmp_path, ids)
    store = AtomicArtifactStore(paths.agent_root)
    store.write_text(Path("decision-cards/keep.md"), "active before\n")
    store.write_text(Path("decision-cards/.archive/old.md"), "archived before\n")
    store.write_text(Path("curator-state/schedule.json"), '{"before": true}\n')
    snapshots = SnapshotStore(paths, store)
    snapshot = snapshots.create(ids)

    store.resolve(Path("decision-cards/keep.md")).unlink()
    store.write_text(Path("decision-cards/.archive/keep.md"), "moved by curator\n")
    store.write_text(Path("decision-cards/.archive/new.md"), "new archive\n")
    store.write_text(Path("curator-state/schedule.json"), '{"after": true}\n')
    store.write_text(Path("curator-state/ai.json"), '{"new": true}\n')
    store.write_text(Path("users/alice/USER_PERSONA.md"), "created candidate\n")

    snapshots.restore(snapshot.snapshot_id, ids)

    assert store.read_text(Path("decision-cards/keep.md")) == "active before\n"
    assert store.read_text(Path("decision-cards/.archive/old.md")) == "archived before\n"
    assert not store.resolve(Path("decision-cards/.archive/keep.md")).exists()
    assert not store.resolve(Path("decision-cards/.archive/new.md")).exists()
    assert store.read_text(Path("curator-state/schedule.json")) == '{"before": true}\n'
    assert not store.resolve(Path("curator-state/ai.json")).exists()
    assert not store.resolve(Path("users/alice/USER_PERSONA.md")).exists()
