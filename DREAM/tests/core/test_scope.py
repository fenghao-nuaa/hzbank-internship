from pathlib import Path

import pytest

from dream.core.scope import ScopeIds, resolve_scope


def test_resolve_scope_keeps_user_under_agent(tmp_path: Path) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    assert paths.user_root == (
        tmp_path / "tenants" / "acme" / "agents" / "assistant" / "users" / "alice"
    )
    assert paths.skills_dir == (
        tmp_path / "tenants" / "acme" / "agents" / "assistant" / "skills"
    )


@pytest.mark.parametrize("value", ["../bob", "/tmp/bob", "a/b", "", "."])
def test_scope_rejects_unsafe_identifiers(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError):
        resolve_scope(tmp_path, ScopeIds("acme", "assistant", value))
