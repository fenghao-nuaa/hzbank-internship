"""Coordinate dream candidates, manual writeback, activation, and rollback."""

import hashlib
from datetime import datetime
from pathlib import Path

from dream.memory.artifacts import AtomicArtifactStore
from dream.application.deadline import DreamDeadline, DreamDeadlineExceeded
from dream.application.service import DreamService
from dream.governance.candidates import GovernanceCandidateStore
from dream.governance.policy import (
    GovernanceArtifact,
    GovernanceMode,
    MemoryGovernancePolicy,
)
from dream.memory.managers.decision_cards import DecisionCardManager
from dream.memory.managers.persona import MemoryManager
from dream.memory.managers.skill_candidates import SkillManager
from dream.memory.items import memory_id_for
from dream.memory.publication import (
    PublicationStatus,
    PublicationStore,
    PublicationTransitionError,
    PublicationVersion,
)
from dream.memory.storage.reports import DreamReportStore
from dream.extraction.models import ArtifactKind, ReviewAction
from dream.core.scope import ScopeIds, resolve_scope
from dream.memory.storage.snapshots import SnapshotStore
from dream.memory.writeback import WritebackBackend, WritebackService


class ClosedLoopError(RuntimeError):
    """A candidate failed without exposing provider details."""


class TaskStartBlocked(RuntimeError):
    def __init__(self, latest_event_id: str, active_event_id: str) -> None:
        self.latest_event_id = latest_event_id
        self.active_event_id = active_event_id
        super().__init__(
            f"latest event {latest_event_id} is not active; active event is "
            f"{active_event_id or 'none'}"
        )


class ClosedLoopCoordinator:
    def __init__(
        self,
        service: DreamService,
        *,
        writeback_backend: WritebackBackend,
        character_limit: int = 3200,
        user_persona_limit: int = 1200,
        deadline_seconds: float = 300.0,
        governance_policy: MemoryGovernancePolicy | None = None,
    ) -> None:
        self.service = service
        self.writeback_backend = writeback_backend
        self.character_limit = character_limit
        self.user_persona_limit = user_persona_limit
        self.governance_policy = governance_policy or MemoryGovernancePolicy()
        if deadline_seconds <= 0:
            raise ValueError("dream deadline must be positive")
        self.deadline_seconds = deadline_seconds

    def _paths(self, ids: ScopeIds):
        return resolve_scope(self.service.home, ids)

    def publications(self, ids: ScopeIds) -> PublicationStore:
        return PublicationStore(self._paths(ids))

    def _snapshots(self, ids: ScopeIds) -> SnapshotStore:
        paths = self._paths(ids)
        return SnapshotStore(paths, AtomicArtifactStore(paths.agent_root))

    def _writebacks(self, ids: ScopeIds) -> WritebackService:
        return WritebackService(
            self._paths(ids),
            backend=self.writeback_backend,
            character_limit=self.character_limit,
            user_persona_limit=self.user_persona_limit,
        )

    def _candidate_store(self, ids: ScopeIds) -> GovernanceCandidateStore:
        return GovernanceCandidateStore(self._paths(ids))

    def dream(self, ids: ScopeIds) -> PublicationVersion:
        with self.service.transaction_lock:
            return self._dream_locked(ids)

    def _dream_locked(self, ids: ScopeIds) -> PublicationVersion:
        publications = self.publications(ids)
        pending = publications.pending_event_ids()
        if not pending:
            raise ValueError("no completed events are waiting for a dream")
        snapshots = self._snapshots(ids)
        before = snapshots.create(ids)
        version = publications.begin(pending, pending[-1], before.snapshot_id)
        deadline = DreamDeadline(self.deadline_seconds)
        try:
            deadline.checkpoint()
            version = publications.mark_dreaming(version.version)
            reviews = self.service.run_pending(ids, deadline=deadline)
            if not reviews or any(run["status"] != "success" for run in reviews):
                raise RuntimeError("background review failed")
            current_governance_artifacts = tuple(
                GovernanceArtifact.from_dict(artifact)
                for run in reviews
                for artifact in run.get("governance_artifacts", [])
                if isinstance(artifact, dict)
            )
            candidate_store = self._candidate_store(ids)
            governance_artifacts = candidate_store.reinforce(
                current_governance_artifacts
            )
            governance = self.governance_policy.decide_all(governance_artifacts)
            deadline.checkpoint()
            if governance.mode is GovernanceMode.OBSERVE:
                candidate_store.record(governance_artifacts)
                snapshots.restore(before.snapshot_id, ids)
                after = snapshots.create(ids)
                character_hash, persona_hash = self._current_writeback_hashes(ids)
                ready = publications.mark_ready_for_activation(
                    version.version,
                    after.snapshot_id,
                    character_hash,
                    persona_hash,
                )
                active = publications.activate(ready.version)
                self._report(
                    ids,
                    active,
                    "observation stored; active memory unchanged",
                    transaction_committed=True,
                    governance_mode=governance.mode.value,
                    risk_level=governance.risk_level.value,
                    governance_reason=governance.reason,
                    governance_artifact_count=len(governance_artifacts),
                )
                return active
            self._apply_reinforced_evidence(
                ids,
                current=current_governance_artifacts,
                reinforced=governance_artifacts,
            )
            writeback = self._writebacks(ids).generate()
            deadline.checkpoint()
            after = snapshots.create(ids)
            governance_details = {
                "governance_mode": governance.mode.value,
                "risk_level": governance.risk_level.value,
                "governance_reason": governance.reason,
                "governance_artifact_count": len(governance_artifacts),
            }
            if governance.mode is GovernanceMode.AUTO_ACTIVATE:
                ready = publications.mark_ready_for_activation(
                    version.version,
                    after.snapshot_id,
                    writeback.character.sha256,
                    writeback.user_persona.sha256,
                )
                self._report(
                    ids,
                    ready,
                    "candidate ready for automatic activation",
                    transaction_committed=True,
                    **governance_details,
                )
                active = publications.activate(ready.version)
                candidate_store.remove(governance_artifacts)
                self._report(
                    ids,
                    active,
                    "version automatically activated",
                    transaction_committed=True,
                    **governance_details,
                )
                return active
            ready = publications.mark_ready_for_review(
                version.version,
                after.snapshot_id,
                writeback.character.sha256,
                writeback.user_persona.sha256,
            )
            self._report(
                ids,
                ready,
                "candidate requires review",
                transaction_committed=True,
                candidate_staged=True,
                **governance_details,
            )
            snapshots.restore(before.snapshot_id, ids)
            return ready
        except Exception as exc:
            snapshots.restore(before.snapshot_id, ids)
            for event_id in pending:
                self.service.review_progress.invalidate(event_id)
            self.service.recover_pending()
            failed = publications.fail(version.version, type(exc).__name__)
            self._report(
                ids,
                failed,
                "candidate timed out"
                if isinstance(exc, DreamDeadlineExceeded)
                else "candidate failed",
                timed_out=isinstance(exc, DreamDeadlineExceeded),
                elapsed_seconds=deadline.elapsed,
                transaction_rolled_back=True,
            )
            raise ClosedLoopError("dream candidate failed") from exc

    def run_pending(self) -> list[PublicationVersion]:
        scopes: list[ScopeIds] = []
        for event in self.service.ledger.read_all():
            if event.scope in scopes:
                continue
            if self.publications(event.scope).pending_event_ids():
                scopes.append(event.scope)
        return [self.dream(ids) for ids in scopes]

    def run_due_pending(self, now: datetime) -> list[PublicationVersion]:
        return [self.dream(ids) for ids in self.service.due_scopes(now)]

    def _current_writeback_hashes(self, ids: ScopeIds) -> tuple[str, str]:
        paths = self._paths(ids)
        artifacts = AtomicArtifactStore(paths.agent_root)
        character = artifacts.read_text(Path("CHARACTER_DEFINITION.md"))
        persona = artifacts.read_text(Path("users") / ids.user_id / "USER_PERSONA.md")
        return (
            hashlib.sha256(character.encode("utf-8")).hexdigest(),
            hashlib.sha256(persona.encode("utf-8")).hexdigest(),
        )

    def _apply_reinforced_evidence(
        self,
        ids: ScopeIds,
        *,
        current: tuple[GovernanceArtifact, ...],
        reinforced: tuple[GovernanceArtifact, ...],
    ) -> None:
        current_by_key = {
            GovernanceCandidateStore.key_for(value): value for value in current
        }
        paths = self._paths(ids)
        for artifact in reinforced:
            original = current_by_key.get(GovernanceCandidateStore.key_for(artifact))
            if (
                original is None
                or artifact.source_event_ids == original.source_event_ids
            ):
                continue
            payload = dict(artifact.attributes)
            if artifact.artifact_type is ArtifactKind.USER_PROFILE:
                content = str(payload.get("content", "")).strip()
                payload.update(
                    {
                        "action": "replace",
                        "content": content,
                        "old_content": content,
                        "memory_id": memory_id_for(content),
                        "target": "user",
                    }
                )
                manager = MemoryManager(paths)
                tool_name = "memory_manage"
            elif artifact.artifact_type is ArtifactKind.DECISION_CARD:
                manager = DecisionCardManager(paths)
                tool_name = "decision_card_manage"
            elif artifact.artifact_type is ArtifactKind.SKILL:
                manager = SkillManager(paths)
                tool_name = "skill_manage"
            else:
                continue
            manager.apply(
                ReviewAction(
                    kind=artifact.artifact_type,
                    tool_name=tool_name,
                    payload=payload,
                    source_event_id=artifact.source_event_ids[0],
                    source_event_ids=artifact.source_event_ids,
                )
            )

    def approve(self, ids: ScopeIds, version: int) -> PublicationVersion:
        return self.publications(ids).approve(version)

    def confirm_writeback(
        self,
        ids: ScopeIds,
        version: int,
        *,
        character_written: bool,
        user_written: bool,
    ) -> PublicationVersion:
        publications = self.publications(ids)
        candidate = publications.get(version)
        active = publications.active()
        if active is not None:
            character_written = character_written or (
                candidate.character_definition_sha256
                == active.character_definition_sha256
            )
            user_written = user_written or (
                candidate.user_persona_sha256 == active.user_persona_sha256
            )
        return publications.confirm_writeback(
            version,
            character_written=character_written,
            user_written=user_written,
        )

    def activate(self, ids: ScopeIds, version: int) -> PublicationVersion:
        with self.service.transaction_lock:
            publications = self.publications(ids)
            candidate = publications.require_activation_ready(version)
            live = self._snapshots(ids).create(ids)
            try:
                if candidate.after_snapshot_id:
                    self._snapshots(ids).restore(candidate.after_snapshot_id, ids)
                active = publications.activate(version)
            except Exception:
                self._snapshots(ids).restore(live.snapshot_id, ids)
                raise
            self._report(ids, active, "version active")
            return active

    def reject(self, ids: ScopeIds, version: int) -> PublicationVersion:
        publications = self.publications(ids)
        candidate = publications.get(version)
        if candidate.status not in {
            PublicationStatus.READY_FOR_REVIEW,
            PublicationStatus.READY_FOR_ACTIVATION,
            PublicationStatus.READY_FOR_WRITEBACK,
        }:
            raise PublicationTransitionError(
                f"cannot reject publication in {candidate.status.value} state"
            )
        self._snapshots(ids).restore(candidate.before_snapshot_id, ids)
        for event_id in candidate.source_event_ids:
            self.service.review_progress.invalidate(event_id)
        self.service.recover_pending()
        failed = publications.fail(version, "rejected")
        self._report(
            ids,
            failed,
            "candidate rejected",
            transaction_rolled_back=True,
        )
        return failed

    def rollback(self, ids: ScopeIds, version: int) -> PublicationVersion:
        publications = self.publications(ids)
        selected = publications.get(version)
        if not selected.after_snapshot_id:
            raise ValueError("rollback target has no completed snapshot")
        self._snapshots(ids).restore(selected.after_snapshot_id, ids)
        restored = publications.restore_active(version)
        self._report(
            ids,
            restored,
            "active version rolled back",
            transaction_rolled_back=True,
        )
        return restored

    def status(self, ids: ScopeIds) -> dict[str, PublicationVersion | None]:
        publications = self.publications(ids)
        return {"latest": publications.latest(), "active": publications.active()}

    def assert_task_can_start(self, ids: ScopeIds) -> None:
        events = [
            event for event in self.service.ledger.read_all() if event.scope == ids
        ]
        if not events:
            return
        latest = events[-1].event_id
        active = self.publications(ids).active()
        active_event = active.processed_through_event_id if active else ""
        if latest != active_event:
            raise TaskStartBlocked(latest, active_event)

    def _report(
        self,
        ids: ScopeIds,
        version: PublicationVersion,
        summary: str,
        **details: object,
    ) -> None:
        report = {
            "run_id": f"publication-{version.version:06d}-{version.status.value}",
            "curator": "closed_loop",
            "status": version.status.value,
            "version": version.version,
            "source_event_ids": list(version.source_event_ids),
            "before_snapshot_id": version.before_snapshot_id,
            "after_snapshot_id": version.after_snapshot_id,
            "character_definition_sha256": version.character_definition_sha256,
            "user_persona_sha256": version.user_persona_sha256,
            "fallback_version": version.fallback_version,
            "summary": summary,
        }
        report.update(details)
        DreamReportStore(self._paths(ids)).write(report)
