"""Application service connecting short-term conversation input to dreams."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from threading import RLock
from uuid import uuid4
from zoneinfo import ZoneInfo

from dream.memory.artifacts import AtomicArtifactStore
from dream.curators.ai import AICurator
from dream.curators.backend import SemanticCuratorBackend
from dream.curators.schedule import CuratorScheduleStore
from dream.curators.semantic import SemanticCuratorRunner
from dream.curators.user import UserCurator
from dream.retrieval.selector import ContextBudget, ContextSelector
from dream.application.deadline import DreamDeadline
from dream.application.progress import ReviewProgressStore
from dream.application.scheduler import (
    DreamScheduler,
    PendingReviewBatch,
    ReviewSchedulePolicy,
)
from dream.core.events import TaskCompletedEvent
from dream.governance.policy import GovernanceArtifact
from dream.core.ledger import EventLedger
from dream.memory.managers.decision_cards import DecisionCardManager
from dream.memory.managers.persona import MemoryManager
from dream.memory.managers.skill_candidates import SkillManager
from dream.memory.publication import PublicationStatus, PublicationStore
from dream.memory.storage.reports import DreamReportStore
from dream.extraction.backend import DeterministicReviewBackend, ReviewBackend
from dream.extraction.cache import ReviewStageCache
from dream.extraction.models import ArtifactKind
from dream.application.review_orchestrator import BackgroundReviewOrchestrator
from dream.memory.storage.rollback import RollbackService
from dream.core.scope import ScopeIds, resolve_scope
from dream.memory.storage.snapshots import SnapshotStore
from dream.integrations.manual import manual_record_to_event, parse_manual_ndjson
from dream.validation.seeds import parse_seed_jsonl, seed_record_to_event


class DreamService:
    """Owns no Redis state; receives completed conversation batches by API."""

    def __init__(
        self,
        home: Path,
        backend: ReviewBackend | None = None,
        semantic_curator_backend: SemanticCuratorBackend | None = None,
        review_threshold: int = 10,
        review_schedule: ReviewSchedulePolicy | None = None,
        timezone_name: str = "Asia/Shanghai",
        curator_daily_hour: int = 3,
        semantic_curator_enabled: bool = False,
        semantic_curator_interval_hours: float = 168.0,
        semantic_curator_min_idle_hours: float = 2.0,
        dream_deadline_seconds: float = 300.0,
        context_budget: ContextBudget | None = None,
    ) -> None:
        if not 0 <= curator_daily_hour <= 23:
            raise ValueError("curator_daily_hour must be between 0 and 23")
        self.home = home
        self.timezone = ZoneInfo(timezone_name)
        self.curator_daily_hour = curator_daily_hour
        self.semantic_curator_enabled = semantic_curator_enabled
        self.semantic_curator_interval = timedelta(
            hours=semantic_curator_interval_hours
        )
        self.semantic_curator_min_idle = timedelta(
            hours=semantic_curator_min_idle_hours
        )
        if dream_deadline_seconds <= 0:
            raise ValueError("dream deadline must be positive")
        self.dream_deadline_seconds = dream_deadline_seconds
        if self.semantic_curator_interval <= timedelta(0):
            raise ValueError("semantic curator interval must be positive")
        if self.semantic_curator_min_idle <= timedelta(0):
            raise ValueError("semantic curator minimum idle time must be positive")
        if semantic_curator_enabled and semantic_curator_backend is None:
            raise ValueError("enabled semantic curator requires a semantic backend")
        self._busy_lock = RLock()
        self.transaction_lock = RLock()
        self._busy_scopes: set[ScopeIds] = set()
        self.ledger = EventLedger(home / "ledger" / "events.jsonl")
        self.review_progress = ReviewProgressStore(
            home / "ledger" / "reviewed-events.jsonl"
        )
        self.scheduler = DreamScheduler(
            review_threshold=review_threshold,
            policy=review_schedule,
        )
        self.reviewer = BackgroundReviewOrchestrator(
            backend or DeterministicReviewBackend()
        )
        self.review_cache = ReviewStageCache(home)
        self.context_selector = ContextSelector(context_budget)
        self.semantic_curator_backend = semantic_curator_backend
        self.recover_pending()

    def recover_pending(self) -> None:
        for event in self.ledger.read_all():
            if not self.review_progress.contains(event.event_id):
                self.scheduler.enqueue_unless_pending(event)

    def ingest_conversation(self, event: TaskCompletedEvent) -> None:
        paths = resolve_scope(self.home, event.scope)
        self.ledger.append(event)
        self.scheduler.enqueue(event)
        PublicationStore(paths).note_completed_event(event.event_id)

    def import_manual_ndjson(self, text: str) -> dict[str, int]:
        imported = 0
        duplicates = 0
        for record in parse_manual_ndjson(text):
            event = manual_record_to_event(record)
            if self.ledger.contains(event.event_id):
                duplicates += 1
                continue
            self.ingest_conversation(event)
            imported += 1
        return {"imported": imported, "duplicates": duplicates}

    def import_ai_seed_jsonl(self, text: str) -> dict[str, int]:
        imported = 0
        duplicates = 0
        for record in parse_seed_jsonl(text):
            event = seed_record_to_event(record)
            if self.ledger.contains(event.event_id):
                duplicates += 1
                continue
            self.ledger.append(event)
            self.scheduler.enqueue(event)
            imported += 1
        return {"imported": imported, "duplicates": duplicates}

    def start_context(
        self,
        ids: ScopeIds,
        *,
        task_text: str = "",
    ) -> dict[str, object]:
        with self.transaction_lock:
            paths = resolve_scope(self.home, ids)
            artifacts = AtomicArtifactStore(paths.agent_root)
            snapshot = SnapshotStore(paths, artifacts).create(ids)
            user_key = f"users/{ids.user_id}/USER.md"
            cards = [
                snapshot_file.content
                for key, snapshot_file in sorted(snapshot.files.items())
                if key.startswith("decision-cards/")
                and "/.archive/" not in f"/{key}"
                and key.endswith(".md")
            ]
            selected = self.context_selector.select(
                user_repository=snapshot.files[user_key].content,
                user_projection=snapshot.files[
                    f"users/{ids.user_id}/USER_PERSONA.md"
                ].content,
                decision_rules_repository=snapshot.files[
                    "DECISION_RULES.md"
                ].content,
                character_projection=snapshot.files[
                    "CHARACTER_DEFINITION.md"
                ].content,
                decision_cards=tuple(cards),
                query=task_text,
            )
            return {
                "snapshot_id": snapshot.snapshot_id,
                "user_profile": selected.user_profile,
                "decision_rules": selected.decision_rules,
                "decision_cards": list(selected.decision_cards),
                "skills": list(selected.skills),
            }

    def due_scopes(self, now: datetime) -> tuple[ScopeIds, ...]:
        self.recover_pending()
        return self.scheduler.due_scopes(now)

    def run_pending(
        self,
        ids: ScopeIds | None = None,
        *,
        deadline: DreamDeadline | None = None,
    ) -> list[dict[str, object]]:
        """Immediately process pending events, preserving the manual API contract."""

        self.recover_pending()
        events: list[TaskCompletedEvent] = []
        while event := self.scheduler.pop_pending(ids):
            events.append(event)
        return self._run_batches(
            self.scheduler.split_pending_batches(events),
            deadline or DreamDeadline(self.dream_deadline_seconds),
        )

    def run_due_pending(self, now: datetime) -> list[dict[str, object]]:
        """Process only batches eligible under the adaptive background policy."""

        self.recover_pending()
        runs: list[dict[str, object]] = []
        while batch := self.scheduler.pop_due_batch(now):
            batch_runs = self._run_batches(
                (batch,), DreamDeadline(self.dream_deadline_seconds)
            )
            runs.extend(batch_runs)
            if any(run["status"] != "success" for run in batch_runs):
                break
        return runs

    def _run_batches(
        self,
        batches: tuple[PendingReviewBatch, ...],
        deadline: DreamDeadline,
    ) -> list[dict[str, object]]:
        scopes = {batch.scope for batch in batches if batch.events}
        transaction_snapshots = {
            scope: SnapshotStore(
                resolve_scope(self.home, scope),
                AtomicArtifactStore(resolve_scope(self.home, scope).agent_root),
            ).create(scope)
            for scope in scopes
        }
        with self._busy_lock:
            self._busy_scopes.update(scopes)
        try:
            deadline.checkpoint()
            return self._run_batches_locked(batches, deadline)
        except Exception as exc:
            for scope, snapshot in transaction_snapshots.items():
                paths = resolve_scope(self.home, scope)
                SnapshotStore(paths, AtomicArtifactStore(paths.agent_root)).restore(
                    snapshot.snapshot_id, scope
                )
                DreamReportStore(paths).write(
                    {
                        "run_id": f"transaction-recovery-{uuid4().hex}",
                        "curator": "background_review",
                        "status": "failed",
                        "transaction_rolled_back": True,
                        "errors": [type(exc).__name__],
                    }
                )
            for batch in batches:
                for event in batch.events:
                    self.review_progress.invalidate(event.event_id)
                    self.scheduler.enqueue_unless_pending(event)
            raise
        finally:
            with self._busy_lock:
                self._busy_scopes.difference_update(scopes)

    def _run_batches_locked(
        self,
        batches: tuple[PendingReviewBatch, ...],
        deadline: DreamDeadline,
    ) -> list[dict[str, object]]:
        runs: list[dict[str, object]] = []
        for batch in batches:
            deadline.checkpoint()
            events = batch.events
            if not events:
                continue
            scope = batch.scope
            paths = resolve_scope(self.home, scope)
            snapshot = SnapshotStore(
                paths, AtomicArtifactStore(paths.agent_root)
            ).create(scope)
            allowed_tools_by_event = {
                event.event_id: (
                    frozenset({"decision_card_manage"})
                    if any(ref.get("source") == "ai-seed" for ref in event.source_refs)
                    else frozenset(
                        {"memory_manage", "decision_card_manage", "skill_manage"}
                    )
                )
                for event in events
            }
            cache_key = self.review_cache.key_for(
                scope,
                events,
                allowed_tools_by_event,
                snapshot,
                self.reviewer.backend,
            )
            cached_result = self.review_cache.load(cache_key) if cache_key else None
            cache_hit = cached_result is not None
            if cached_result is not None:
                result = cached_result
            else:
                result = deadline.call(
                    lambda: self.reviewer.review_batch(
                        events,
                        allowed_tools_by_event=allowed_tools_by_event,
                        snapshot=snapshot,
                    )
                )
            deadline.checkpoint()
            if cache_key is not None and not cache_hit:
                self.review_cache.store(cache_key, result)
            applied_kinds: list[str] = []
            rollback_ids: list[str] = []
            errors: list[str] = []
            for action in result.actions:
                deadline.checkpoint()
                try:
                    if action.kind is ArtifactKind.USER_PROFILE:
                        manager = MemoryManager(paths)
                    elif action.kind is ArtifactKind.DECISION_CARD:
                        manager = DecisionCardManager(paths)
                    elif action.kind is ArtifactKind.SKILL:
                        manager = SkillManager(paths)
                    else:
                        continue
                    manager.apply(action)
                    rollback_ids.append(manager.last_snapshot_id)
                    applied_kinds.append(action.kind.value)
                    deadline.checkpoint()
                except Exception as exc:
                    errors.append(f"{action.kind.value}: {type(exc).__name__}: {exc}")
            if result.error:
                errors.append(result.error)
            run_id = f"review-{uuid4().hex}"
            if result.status == "failed":
                status = "failed"
            elif errors or result.status == "partial":
                status = "partial"
            else:
                status = "success"
            if result.trace is not None:
                DreamReportStore(paths).write_trace(
                    run_id,
                    {
                        **result.trace,
                        "source_event_ids": [event.event_id for event in events],
                    },
                )
            DreamReportStore(paths).write(
                {
                    "run_id": run_id,
                    "curator": "background_review",
                    "status": status,
                    "source_event_ids": [event.event_id for event in events],
                    "artifact_kinds": applied_kinds,
                    "rollback_snapshot_ids": rollback_ids,
                    "errors": errors,
                    "review_summary": result.summary,
                    "event_dispositions": [
                        {
                            "event_id": disposition.event_id,
                            "disposition": disposition.disposition,
                            "reason": disposition.reason,
                        }
                        for disposition in result.event_dispositions
                    ],
                    "transaction_rolled_back": status != "success",
                    "semantic_cache_hit": cache_hit,
                    "semantic_input_hash": (
                        cache_key.input_hash if cache_key is not None else None
                    ),
                    "governance_artifacts": [
                        GovernanceArtifact.from_review_action(action).to_dict()
                        for action in result.actions
                    ],
                }
            )
            if status == "success":
                for event in events:
                    self.review_progress.append(event.event_id)
                self.scheduler.mark_review_accepted(scope)
            else:
                SnapshotStore(paths, AtomicArtifactStore(paths.agent_root)).restore(
                    snapshot.snapshot_id, scope
                )
                for event in events:
                    self.scheduler.enqueue_unless_pending(event)
            runs.append(
                {
                    "run_id": run_id,
                    "source_event_ids": [event.event_id for event in events],
                    "status": status,
                    "artifact_kinds": applied_kinds,
                    "errors": errors,
                    "curator_runs": [],
                    "semantic_cache_hit": cache_hit,
                    "semantic_input_hash": (
                        cache_key.input_hash if cache_key is not None else None
                    ),
                    "governance_artifacts": [
                        GovernanceArtifact.from_review_action(action).to_dict()
                        for action in result.actions
                    ],
                    "_scope": scope,
                }
            )
        deadline.checkpoint()
        self._run_immediate_curators(runs, deadline)
        for run in runs:
            scope = run.pop("_scope")
            assert isinstance(scope, ScopeIds)
            paths = resolve_scope(self.home, scope)
            report_relative = Path("dream-reports") / f"{run['run_id']}.json"
            artifacts = AtomicArtifactStore(paths.agent_root)
            payload = json.loads(artifacts.read_text(report_relative))
            payload["curator_runs"] = list(run["curator_runs"])
            artifacts.write_text(
                report_relative,
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            )
        return runs

    def _run_immediate_curators(
        self, runs: list[dict[str, object]], deadline: DreamDeadline
    ) -> None:
        successful = [run for run in runs if run["status"] == "success"]
        changed_users: dict[ScopeIds, None] = {}
        changed_agents: dict[tuple[str, str], ScopeIds] = {}
        for run in successful:
            scope = run["_scope"]
            assert isinstance(scope, ScopeIds)
            kinds = set(run["artifact_kinds"])
            if ArtifactKind.USER_PROFILE.value in kinds:
                changed_users[scope] = None
            if ArtifactKind.DECISION_CARD.value in kinds:
                changed_agents[(scope.tenant_id, scope.agent_id)] = scope

        now = datetime.now(timezone.utc)
        for ids in sorted(
            changed_users,
            key=lambda item: (item.tenant_id, item.agent_id, item.user_id),
        ):
            deadline.checkpoint()
            paths = resolve_scope(self.home, ids)
            UserCurator(paths).run()
            deadline.checkpoint()
            CuratorScheduleStore(paths).record_user_success(ids.user_id, now)
            for run in successful:
                if run["_scope"] == ids:
                    run["curator_runs"].append("user")

        for agent_key, ids in sorted(changed_agents.items()):
            deadline.checkpoint()
            paths = resolve_scope(self.home, ids)
            AICurator(paths).run()
            deadline.checkpoint()
            CuratorScheduleStore(paths).record_ai_success(now)
            for run in successful:
                scope = run["_scope"]
                assert isinstance(scope, ScopeIds)
                if (
                    scope.tenant_id,
                    scope.agent_id,
                ) == agent_key and ArtifactKind.DECISION_CARD.value in set(
                    run["artifact_kinds"]
                ):
                    run["curator_runs"].append("ai")

    def run_curators(self, ids: ScopeIds) -> dict[str, object]:
        paths = resolve_scope(self.home, ids)
        now = datetime.now(timezone.utc)
        ai_report = AICurator(paths).run()
        user_report = UserCurator(paths).run()
        schedule = CuratorScheduleStore(paths)
        schedule.record_ai_success(now)
        schedule.record_user_success(ids.user_id, now)
        return {"ai": ai_report, "user": user_report}

    def run_due_curators(self, now: datetime) -> dict[str, dict[str, object]]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must include a timezone")
        active_scopes = {event.scope for event in self.ledger.read_all()}
        agents: dict[tuple[str, str], list[ScopeIds]] = {}
        for ids in active_scopes:
            agents.setdefault((ids.tenant_id, ids.agent_id), []).append(ids)
        results: dict[str, dict[str, object]] = {}
        for _, scopes in sorted(agents.items()):
            scopes.sort(key=lambda item: item.user_id)
            representative = scopes[0]
            paths = resolve_scope(self.home, representative)
            schedule = CuratorScheduleStore(paths)
            if not schedule.daily_due(now, self.timezone, self.curator_daily_hour):
                continue
            with self._busy_lock:
                if any(scope in self._busy_scopes for scope in scopes):
                    continue

            processed: list[str] = []
            errors: list[str] = []
            if schedule.ai_changed():
                try:
                    report = AICurator(paths).run()
                    schedule.record_ai_success(now)
                    key = self._scope_key(representative)
                    results.setdefault(key, {})["ai"] = report
                    processed.append(key)
                except Exception as exc:
                    errors.append(f"ai: {type(exc).__name__}: {exc}")
            for ids in scopes:
                if not schedule.user_changed(ids.user_id):
                    continue
                try:
                    report = UserCurator(resolve_scope(self.home, ids)).run()
                    schedule.record_user_success(ids.user_id, now)
                    key = self._scope_key(ids)
                    results.setdefault(key, {})["user"] = report
                    processed.append(key)
                except Exception as exc:
                    errors.append(f"user/{ids.user_id}: {type(exc).__name__}: {exc}")
            schedule.record_daily_check(
                now,
                self.timezone,
                processed_scopes=tuple(dict.fromkeys(processed)),
                errors=tuple(errors),
            )
        return results

    def run_due_semantic_curators(self, now: datetime) -> dict[str, dict[str, object]]:
        if not self.semantic_curator_enabled:
            return {}
        if self.semantic_curator_backend is None:
            return {}
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must include a timezone")

        events = self.ledger.read_all()
        agents: dict[tuple[str, str], list[ScopeIds]] = {}
        for event in events:
            key = (event.scope.tenant_id, event.scope.agent_id)
            scopes = agents.setdefault(key, [])
            if event.scope not in scopes:
                scopes.append(event.scope)

        results: dict[str, dict[str, object]] = {}
        for agent_key, scopes in sorted(agents.items()):
            scopes.sort(key=lambda item: item.user_id)
            representative = scopes[0]
            paths = resolve_scope(self.home, representative)
            schedule = CuratorScheduleStore(paths)
            if not schedule.semantic_due(now, self.semantic_curator_interval):
                continue
            if self._agent_is_busy(scopes):
                continue
            if self._ordinary_dream_occupies(scopes):
                continue
            agent_events = [
                event
                for event in events
                if (event.scope.tenant_id, event.scope.agent_id) == agent_key
            ]
            latest_activity = max(
                self._completed_at(event.completed_at) for event in agent_events
            )
            if (
                now.astimezone(timezone.utc) - latest_activity
                < self.semantic_curator_min_idle
            ):
                continue
            if not self._has_semantic_candidates(paths, scopes):
                continue

            runner = SemanticCuratorRunner(
                home=self.home,
                ids=representative,
                user_ids=tuple(ids.user_id for ids in scopes),
                backend=self.semantic_curator_backend,
            )
            report = runner.run(now)
            schedule.record_semantic_attempt(
                now,
                run_id=str(report["run_id"]),
                status=str(report["status"]),
                candidate_path=str(report["candidate_path"]),
                error=str(report["error"]),
            )
            results[f"{agent_key[0]}/{agent_key[1]}"] = report
        return results

    def _agent_is_busy(self, scopes: list[ScopeIds]) -> bool:
        with self._busy_lock:
            return any(scope in self._busy_scopes for scope in scopes)

    def _ordinary_dream_occupies(self, scopes: list[ScopeIds]) -> bool:
        occupied = {
            PublicationStatus.PENDING,
            PublicationStatus.DREAMING,
            PublicationStatus.READY_FOR_REVIEW,
            PublicationStatus.READY_FOR_WRITEBACK,
        }
        for ids in scopes:
            if self.scheduler.pending_event_ids(ids):
                return True
            latest = PublicationStore(resolve_scope(self.home, ids)).latest()
            if latest is not None and latest.status in occupied:
                return True
        return False

    @staticmethod
    def _completed_at(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("completed_at must include a timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _has_semantic_candidates(paths, scopes: list[ScopeIds]) -> bool:
        if len(list(paths.decision_cards_dir.glob("*.md"))) >= 2:
            return True
        artifacts = AtomicArtifactStore(paths.agent_root)
        for ids in scopes:
            profile = artifacts.read_text(Path("users") / ids.user_id / "USER.md")
            entries = [entry for entry in profile.split("\n§\n") if entry.strip()]
            if len(entries) >= 2:
                return True
        return False

    @staticmethod
    def _scope_key(ids: ScopeIds) -> str:
        return f"{ids.tenant_id}/{ids.agent_id}/{ids.user_id}"

    def rollback(self, ids: ScopeIds, snapshot_id: str) -> None:
        paths = resolve_scope(self.home, ids)
        RollbackService(paths).restore(snapshot_id)

    def read_report(self, ids: ScopeIds, run_id: str) -> str:
        paths = resolve_scope(self.home, ids)
        return AtomicArtifactStore(paths.agent_root).read_text(
            Path("dream-reports") / f"{run_id}.json"
        )
