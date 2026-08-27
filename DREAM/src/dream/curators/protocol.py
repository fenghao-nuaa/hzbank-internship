"""Shared curator result and scheduling protocol."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class CuratorRunReport:
    run_id: str
    curator: str
    status: str
    changed: int
    archived: int
    rollback_snapshot_id: str
    report_path: str


class Curator(Protocol):
    name: str

    def should_run(self, now: datetime) -> bool: ...

    def run(self) -> object: ...

