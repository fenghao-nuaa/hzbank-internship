"""Safe, file-scoped retention cleanup for durable journal files."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

from short_term_memory.storage.vfs_adapter import VFSAdapter, safe_component


@dataclass(frozen=True)
class JournalRetentionFailure:
    path: Path
    error: str


@dataclass(frozen=True)
class JournalRetentionResult:
    removed: tuple[Path, ...]
    failures: tuple[JournalRetentionFailure, ...]


class JournalRetentionJob:
    def __init__(self, vfs: VFSAdapter, *, retention_days: int = 30) -> None:
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        self.vfs = vfs
        self.retention_days = retention_days

    def run(self, now: datetime) -> JournalRetentionResult:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must include a timezone")
        cutoff = now.astimezone(timezone.utc) - timedelta(days=self.retention_days)
        removed: list[Path] = []
        failures: list[JournalRetentionFailure] = []
        for path in sorted(self.vfs.root.glob("*/journals/*.jsonl")):
            if not path.is_file() or not self._has_date_prefix(path.name):
                continue
            try:
                latest = self._latest_valid_timestamp(path)
            except (OSError, UnicodeError) as error:
                failures.append(JournalRetentionFailure(path, str(error)))
                continue
            if latest is None:
                failures.append(
                    JournalRetentionFailure(path, "no valid journal timestamps")
                )
                continue
            if latest < cutoff:
                try:
                    path.unlink()
                except OSError as error:
                    failures.append(JournalRetentionFailure(path, str(error)))
                else:
                    removed.append(path)
        return JournalRetentionResult(tuple(removed), tuple(failures))

    @staticmethod
    def _has_date_prefix(filename: str) -> bool:
        if len(filename) < 17 or filename[10] != "-" or not filename.endswith(".jsonl"):
            return False
        try:
            date.fromisoformat(filename[:10])
            safe_component(filename[11:-6], "session_id")
        except ValueError:
            return False
        return True

    @staticmethod
    def _latest_valid_timestamp(path: Path) -> datetime | None:
        latest: datetime | None = None
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    raw = json.loads(line)
                    timestamp = datetime.fromisoformat(raw["timestamp"])
                    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                        continue
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                candidate = timestamp.astimezone(timezone.utc)
                if latest is None or candidate > latest:
                    latest = candidate
        return latest
