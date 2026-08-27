"""Periodic dream that consolidates decision cards into AI decision rules."""

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


_PRINCIPLE = re.compile(r"## 决策原则\s*\n\s*(.+?)(?=\n\s*## )", re.DOTALL)


class AICurator:
    name = "ai"

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
        return Path("curator-state") / "ai.json"

    def should_run(self, now: datetime) -> bool:
        raw = self.artifacts.read_text(self._state_relative())
        if not raw:
            return True
        last_run = datetime.fromisoformat(str(json.loads(raw)["last_run_at"]))
        return now - last_run >= self.interval

    def run(self) -> CuratorRunReport:
        cards = sorted(
            path
            for path in self.paths.decision_cards_dir.glob("*.md")
            if path.is_file()
        )
        card_records: list[tuple[Path, str, str, str]] = []
        for card in cards:
            content = card.read_text(encoding="utf-8")
            match = _PRINCIPLE.search(content)
            if match:
                card_records.append(
                    (card, card.stem, match.group(1).strip(), content)
                )
        grouped: dict[str, list[tuple[Path, str, str]]] = {}
        for record in card_records:
            normalized = " ".join(record[2].split()).casefold()
            grouped.setdefault(normalized, []).append(record[:3])

        relative_cards = tuple(
            card.relative_to(self.paths.agent_root)
            for card, _, _, _ in card_records
        )
        rollback = RollbackService(self.paths)
        snapshot_id = rollback.capture((Path("DECISION_RULES.md"),) + relative_cards)

        archived = 0
        summary = "Deterministic exact-principle consolidation."
        if self.semantic_backend is not None and card_records:
            plan = self.semantic_backend.curate_ai(
                cards=tuple((card_id, content) for _, card_id, _, content in card_records),
                current_rules=self.artifacts.read_text(Path("DECISION_RULES.md")),
            )
            active_ids = {card_id for _, card_id, _, _ in card_records}
            archive_ids = set(plan.archive_card_ids)
            if not archive_ids <= active_ids:
                raise ValueError("AI Curator attempted to archive an unknown card")
            if active_ids and archive_ids == active_ids:
                raise ValueError("AI Curator cannot archive every active decision card")
            rules_text = plan.decision_rules_markdown.strip()
            if not rules_text:
                raise ValueError("AI Curator returned empty decision rules")
            for card, card_id, _, content in card_records:
                if card_id in archive_ids:
                    self.artifacts.write_text(
                        Path("decision-cards") / ".archive" / card.name,
                        content,
                    )
                    card.unlink()
                    archived += 1
            self.artifacts.write_text(Path("DECISION_RULES.md"), rules_text + "\n")
            changed = 1
            summary = plan.summary
        else:
            rules: list[str] = [
                "# AI Decision Rules",
                "",
                "由 AI Curator 从决策卡定期提炼。",
                "",
            ]
            for records in grouped.values():
                principle = records[0][2]
                card_ids = [record[1] for record in records]
                rules.append(f"- {principle}")
                rules.append(f"  - Evidence cards: {', '.join(card_ids)}")
                for duplicate, _, _ in records[1:]:
                    archive_relative = (
                        Path("decision-cards") / ".archive" / duplicate.name
                    )
                    self.artifacts.write_text(
                        archive_relative, duplicate.read_text(encoding="utf-8")
                    )
                    duplicate.unlink()
                    archived += 1
            self.artifacts.write_text(
                Path("DECISION_RULES.md"), "\n".join(rules) + "\n"
            )
            changed = len(grouped)
        now = datetime.now(timezone.utc)
        self.artifacts.write_text(
            self._state_relative(),
            json.dumps({"last_run_at": now.isoformat()}, indent=2) + "\n",
        )
        run_id = f"ai-{uuid4().hex}"
        report_payload: dict[str, object] = {
            "run_id": run_id,
            "curator": self.name,
            "status": "success",
            "changed": changed,
            "archived": archived,
            "rollback_snapshot_id": snapshot_id,
            "card_ids": [record[1] for record in card_records],
            "summary": summary,
        }
        report_path = DreamReportStore(self.paths).write(report_payload)
        return CuratorRunReport(
            run_id=run_id,
            curator=self.name,
            status="success",
            changed=changed,
            archived=archived,
            rollback_snapshot_id=snapshot_id,
            report_path=str(report_path),
        )
