"""Periodic dream that consolidates one isolated user's profile evidence."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from uuid import uuid4

from dream.memory.artifacts import AtomicArtifactStore
from dream.curators.backend import SemanticCuratorBackend
from dream.curators.protocol import CuratorRunReport
from dream.memory.storage.reports import DreamReportStore
from dream.memory.storage.rollback import RollbackService
from dream.core.scope import ScopePaths


_ENTRY_DELIMITER = "\n§\n"
_SOURCE = re.compile(r"<!-- dream-sources?:\s*([^>]+) -->")


class UserCurator:
    name = "user"

    def __init__(
        self,
        paths: ScopePaths,
        interval_hours: int = 168,
        semantic_backend: SemanticCuratorBackend | None = None,
    ) -> None:
        self.paths = paths
        self.interval = timedelta(hours=interval_hours)
        self.artifacts = AtomicArtifactStore(paths.agent_root)
        self.semantic_backend = semantic_backend

    def _state_relative(self) -> Path:
        return Path("curator-state") / f"user-{self.paths.user_root.name}.json"

    def _profile_relative(self) -> Path:
        return Path("users") / self.paths.user_root.name / "USER.md"

    def should_run(self, now: datetime) -> bool:
        raw = self.artifacts.read_text(self._state_relative())
        if not raw:
            return True
        last_run = datetime.fromisoformat(str(json.loads(raw)["last_run_at"]))
        return now - last_run >= self.interval

    def run(self) -> CuratorRunReport:
        relative = self._profile_relative()
        existing = self.artifacts.read_text(relative)
        summary = "Deterministic exact-profile consolidation."
        if self.semantic_backend is not None and existing.strip():
            plan = self.semantic_backend.curate_user(existing)
            rendered = plan.user_profile_markdown.strip() + "\n"
            existing_sources = {
                value.strip()
                for match in _SOURCE.finditer(existing)
                for value in match.group(1).split(",")
                if value.strip()
            }
            rendered_sources = {
                value.strip()
                for match in _SOURCE.finditer(rendered)
                for value in match.group(1).split(",")
                if value.strip()
            }
            if rendered_sources != existing_sources:
                raise ValueError("User Curator must preserve exactly the existing evidence IDs")
            rendered_entries = [
                item for item in rendered.split(_ENTRY_DELIMITER) if item.strip()
            ]
            summary = plan.summary
        else:
            groups: dict[str, dict[str, object]] = {}
            for entry in (item.strip() for item in existing.split(_ENTRY_DELIMITER)):
                if not entry:
                    continue
                source_match = _SOURCE.search(entry)
                base = _SOURCE.sub("", entry).strip()
                key = " ".join(base.split()).casefold()
                record = groups.setdefault(key, {"content": base, "sources": []})
                if source_match:
                    sources = [
                        value.strip()
                        for value in source_match.group(1).split(",")
                        if value.strip()
                    ]
                    current = record["sources"]
                    assert isinstance(current, list)
                    for source in sources:
                        if source not in current:
                            current.append(source)
            rendered_entries = []
            for record in groups.values():
                content = str(record["content"])
                sources = record["sources"]
                assert isinstance(sources, list)
                citation = (
                    f"\n<!-- dream-sources: {', '.join(sources)} -->"
                    if sources
                    else ""
                )
                rendered_entries.append(content + citation)
            rendered = _ENTRY_DELIMITER.join(rendered_entries)
            if rendered:
                rendered += "\n"

        rollback = RollbackService(self.paths)
        snapshot_id = rollback.capture((relative,))
        changed = int(rendered != existing)
        if changed:
            self.artifacts.write_text(relative, rendered)
        now = datetime.now(timezone.utc)
        self.artifacts.write_text(
            self._state_relative(),
            json.dumps({"last_run_at": now.isoformat()}, indent=2) + "\n",
        )
        run_id = f"user-{uuid4().hex}"
        report_payload: dict[str, object] = {
            "run_id": run_id,
            "curator": self.name,
            "status": "success",
            "changed": changed,
            "archived": 0,
            "rollback_snapshot_id": snapshot_id,
            "entry_count": len(rendered_entries),
            "summary": summary,
        }
        report_path = DreamReportStore(self.paths).write(report_payload)
        return CuratorRunReport(
            run_id=run_id,
            curator=self.name,
            status="success",
            changed=changed,
            archived=0,
            rollback_snapshot_id=snapshot_id,
            report_path=str(report_path),
        )
