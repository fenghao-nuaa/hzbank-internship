"""Strict path resolution for tenant, agent, and user memory scopes."""

from dataclasses import dataclass
from pathlib import Path
import re


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class ScopeIds:
    tenant_id: str
    agent_id: str
    user_id: str


@dataclass(frozen=True)
class ScopePaths:
    agent_root: Path
    user_root: Path
    skills_dir: Path
    decision_cards_dir: Path
    snapshots_dir: Path
    reports_dir: Path


def _validate(value: str, label: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def resolve_scope(home: Path, ids: ScopeIds) -> ScopePaths:
    """Resolve a scope without accepting caller-controlled path fragments."""

    tenant = _validate(ids.tenant_id, "tenant_id")
    agent = _validate(ids.agent_id, "agent_id")
    user = _validate(ids.user_id, "user_id")
    agent_root = home / "tenants" / tenant / "agents" / agent
    user_root = agent_root / "users" / user
    return ScopePaths(
        agent_root=agent_root,
        user_root=user_root,
        skills_dir=agent_root / "skills",
        decision_cards_dir=agent_root / "decision-cards",
        snapshots_dir=agent_root / "snapshots",
        reports_dir=agent_root / "dream-reports",
    )
