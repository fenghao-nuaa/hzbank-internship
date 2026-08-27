"""Isolated semantic Curator candidates that never overwrite active artifacts."""

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from dream.memory.artifacts import AtomicArtifactStore
from dream.curators.ai import AICurator
from dream.curators.backend import SemanticCuratorBackend
from dream.curators.user import UserCurator
from dream.memory.storage.reports import DreamReportStore
from dream.core.scope import ScopeIds, ScopePaths, resolve_scope


class SemanticCuratorRunner:
    def __init__(
        self,
        *,
        home: Path,
        ids: ScopeIds,
        user_ids: tuple[str, ...],
        backend: SemanticCuratorBackend,
    ) -> None:
        self.home = home
        self.ids = ids
        self.user_ids = user_ids
        self.backend = backend
        self.active_paths = resolve_scope(home, ids)

    def run(self, now: datetime) -> dict[str, object]:
        run_id = f"semantic-{uuid4().hex}"
        candidate_path = ""
        error = ""
        status = "failed"
        try:
            with tempfile.TemporaryDirectory(prefix="dream-semantic-") as directory:
                work_home = Path(directory)
                work_paths = resolve_scope(work_home, self.ids)
                self._copy_active_to_work(work_paths)
                if any(work_paths.decision_cards_dir.glob("*.md")):
                    AICurator(work_paths, semantic_backend=self.backend).run()
                for user_id in self.user_ids:
                    user_ids = ScopeIds(
                        self.ids.tenant_id,
                        self.ids.agent_id,
                        user_id,
                    )
                    user_paths = resolve_scope(work_home, user_ids)
                    profile = user_paths.user_root / "USER.md"
                    if profile.exists() and profile.read_text(encoding="utf-8").strip():
                        UserCurator(
                            user_paths,
                            semantic_backend=self.backend,
                        ).run()
                candidate = self._persist_candidate(work_paths, run_id, now)
                candidate_path = str(candidate)
                status = "candidate_ready"
        except Exception as exc:
            error = type(exc).__name__

        report_path = DreamReportStore(self.active_paths).write(
            {
                "run_id": run_id,
                "curator": "semantic",
                "status": status,
                "tenant_id": self.ids.tenant_id,
                "agent_id": self.ids.agent_id,
                "user_ids": list(self.user_ids),
                "candidate_path": candidate_path,
                "error": error,
                "summary": (
                    "Semantic candidate generated in an isolated working copy."
                    if status == "candidate_ready"
                    else "Semantic candidate failed; active artifacts were unchanged."
                ),
            }
        )
        return {
            "run_id": run_id,
            "status": status,
            "candidate_path": candidate_path,
            "report_path": str(report_path),
            "error": error,
        }

    def _copy_active_to_work(self, work_paths: ScopePaths) -> None:
        self._copy_file(
            self.active_paths.agent_root / "DECISION_RULES.md",
            work_paths.agent_root / "DECISION_RULES.md",
        )
        for card in self.active_paths.decision_cards_dir.glob("*.md"):
            self._copy_file(card, work_paths.decision_cards_dir / card.name)
        for user_id in self.user_ids:
            self._copy_file(
                self.active_paths.agent_root / "users" / user_id / "USER.md",
                work_paths.agent_root / "users" / user_id / "USER.md",
            )

    @staticmethod
    def _copy_file(source: Path, target: Path) -> None:
        if not source.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    def _persist_candidate(
        self, work_paths: ScopePaths, run_id: str, now: datetime
    ) -> Path:
        root = self.active_paths.agent_root / "semantic-curator-candidates"
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{run_id}-", dir=root))
        final = root / run_id
        try:
            self._copy_file(
                work_paths.agent_root / "DECISION_RULES.md",
                staging / "DECISION_RULES.md",
            )
            if work_paths.decision_cards_dir.exists():
                for card in work_paths.decision_cards_dir.rglob("*.md"):
                    self._copy_file(
                        card,
                        staging
                        / "decision-cards"
                        / card.relative_to(work_paths.decision_cards_dir),
                    )
            for user_id in self.user_ids:
                self._copy_file(
                    work_paths.agent_root / "users" / user_id / "USER.md",
                    staging / "users" / user_id / "USER.md",
                )
            AtomicArtifactStore(staging).write_text(
                Path("manifest.json"),
                json.dumps(
                    {
                        "run_id": run_id,
                        "status": "candidate_ready",
                        "created_at": now.isoformat(),
                        "tenant_id": self.ids.tenant_id,
                        "agent_id": self.ids.agent_id,
                        "user_ids": list(self.user_ids),
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
            os.replace(staging, final)
            return final
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
