"""Per-run JSON reports for audit and rollback discovery."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from dream.memory.artifacts import AtomicArtifactStore
from dream.core.scope import ScopePaths


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class DreamReportStore:
    def __init__(self, paths: ScopePaths) -> None:
        self.paths = paths
        self.artifacts = AtomicArtifactStore(paths.agent_root)

    def write(self, report: dict[str, object]) -> Path:
        run_id = str(report.get("run_id", ""))
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("invalid run_id")
        payload = dict(report)
        payload.setdefault("written_at", datetime.now(timezone.utc).isoformat())
        relative = Path("dream-reports") / f"{run_id}.json"
        self.artifacts.write_text(
            relative,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return self.artifacts.resolve(relative)

    def write_trace(self, run_id: str, trace: dict[str, object]) -> Path:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("invalid run_id")
        payload = dict(trace)
        payload.setdefault("written_at", datetime.now(timezone.utc).isoformat())
        relative = Path("dream-reports") / "review-traces" / f"{run_id}.json"
        self.artifacts.write_text(
            relative,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return self.artifacts.resolve(relative)
