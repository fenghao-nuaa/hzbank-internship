from pathlib import Path

import pytest

from short_term_memory.storage.vfs_adapter import VFSAdapter


def test_vfs_creates_only_journals_under_one_user(tmp_path: Path) -> None:
    paths = VFSAdapter(tmp_path).paths("user-001")

    assert paths.root == tmp_path / "user-001"
    assert paths.journals == paths.root / "journals"
    assert paths.directories() == (paths.journals,)
    assert paths.journals.is_dir()
    assert not (paths.root / "raw").exists()
    assert not (paths.root / "source").exists()
    assert not (paths.root / "wiki").exists()


@pytest.mark.parametrize("user_id", ["", ".", "..", "../other", "a/b", "a\\b"])
def test_vfs_rejects_unsafe_user_ids(tmp_path: Path, user_id: str) -> None:
    with pytest.raises(ValueError, match="invalid user_id"):
        VFSAdapter(tmp_path).paths(user_id)


def test_vfs_keeps_users_isolated(tmp_path: Path) -> None:
    adapter = VFSAdapter(tmp_path)
    assert adapter.paths("alice").root != adapter.paths("bob").root
