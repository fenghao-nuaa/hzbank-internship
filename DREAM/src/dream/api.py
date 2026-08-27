"""HTTP adapter between short-term memory and DREAM."""

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request, Response, status
import httpx
from pydantic import BaseModel, ConfigDict, Field

from dream.application.closed_loop import ClosedLoopCoordinator, ClosedLoopError, TaskStartBlocked
from dream.config import (
    build_curator_backend,
    build_review_backend,
    build_writeback_backend,
    load_settings,
)
from dream.core.events import TaskCompletedEvent
from dream.memory.publication import PublicationTransitionError, PublicationVersion
from dream.core.scope import ScopeIds
from dream.application.scheduler import ReviewSchedulePolicy
from dream.application.service import DreamService
from dream.integrations.internship.client import InternshipSourceClient
from dream.integrations.internship.sync import InternshipSourceSync
from dream.integrations.manual import ManualSourceError


class ScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    agent_id: str
    user_id: str

    def to_ids(self) -> ScopeIds:
        return ScopeIds(self.tenant_id, self.agent_id, self.user_id)


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: str


class ConversationRequest(ScopeRequest):
    event_id: str
    conversation_id: str
    completed_at: str
    interrupted: bool = False
    tool_iterations: int = Field(default=10, ge=0)
    headroom_summary: str = ""
    messages: list[ConversationMessage]
    final_response: str

    def to_event(self) -> TaskCompletedEvent:
        transcript: list[dict[str, object]] = [
            message.model_dump() for message in self.messages
        ]
        if self.headroom_summary:
            transcript.append(
                {"role": "headroom_summary", "content": self.headroom_summary}
            )
        return TaskCompletedEvent(
            event_id=self.event_id,
            task_id=self.conversation_id,
            scope=self.to_ids(),
            completed_at=self.completed_at,
            interrupted=self.interrupted,
            tool_iterations=self.tool_iterations,
            transcript=tuple(transcript),
            final_response=self.final_response,
            source_refs=(),
        )


class WritebackConfirmationRequest(ScopeRequest):
    character_definition_written: bool
    user_persona_written: bool


logger = logging.getLogger(__name__)


def create_app(
    home: Path,
    worker_interval_seconds: float = 60.0,
    *,
    env_file: Path | None = None,
    client_factory: Callable[..., object] | None = None,
    source_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    resolved_env_file = env_file if env_file is not None else home / ".env"
    settings = load_settings(resolved_env_file)
    backend = build_review_backend(settings, client_factory=client_factory)
    semantic_curator_backend = build_curator_backend(
        settings, client_factory=client_factory
    )
    writeback_backend = build_writeback_backend(settings, client_factory=client_factory)
    service = DreamService(
        home,
        backend=backend,
        semantic_curator_backend=semantic_curator_backend,
        review_schedule=ReviewSchedulePolicy(
            idle_after=timedelta(hours=settings.review_idle_hours),
            max_batch_tokens=settings.review_max_batch_tokens,
            max_batch_events=settings.review_max_batch_events,
            max_wait=timedelta(hours=settings.review_max_wait_hours),
        ),
        timezone_name=settings.timezone,
        curator_daily_hour=settings.curator_daily_hour,
        semantic_curator_enabled=settings.curator_consolidate,
        semantic_curator_interval_hours=(settings.curator_consolidate_interval_hours),
        semantic_curator_min_idle_hours=(settings.curator_consolidate_min_idle_hours),
        dream_deadline_seconds=settings.dream_deadline_seconds,
    )
    closed_loop = ClosedLoopCoordinator(
        service,
        writeback_backend=writeback_backend,
        character_limit=settings.character_definition_limit,
        user_persona_limit=settings.user_persona_limit,
        deadline_seconds=settings.dream_deadline_seconds,
    )
    source_sync: InternshipSourceSync | None = None
    if settings.internship_source.enabled:
        source_sync = InternshipSourceSync(
            service,
            settings.internship_source,
            client=InternshipSourceClient(
                settings.internship_source,
                transport=source_transport,
            ),
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop = asyncio.Event()

        async def dream_worker() -> None:
            while not stop.is_set():
                if source_sync is not None:
                    try:
                        source_result = await asyncio.to_thread(
                            source_sync.sync_if_due, datetime.now(timezone.utc)
                        )
                        if source_result.status == "error":
                            logger.warning(
                                "DREAM source sync failed: %s",
                                "; ".join(source_result.errors),
                            )
                    except Exception:
                        logger.exception("DREAM source sync iteration failed")
                if not settings.validation_require_active_writeback:
                    try:
                        await asyncio.to_thread(
                            closed_loop.run_due_pending, datetime.now(timezone.utc)
                        )
                        await asyncio.to_thread(
                            service.run_due_curators, datetime.now(timezone.utc)
                        )
                        await asyncio.to_thread(
                            service.run_due_semantic_curators,
                            datetime.now(timezone.utc),
                        )
                    except Exception:
                        logger.exception("DREAM background worker iteration failed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=worker_interval_seconds)
                except TimeoutError:
                    continue

        worker = asyncio.create_task(dream_worker())
        try:
            yield
        finally:
            stop.set()
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    application = FastAPI(title="DREAM", version="0.1.0", lifespan=lifespan)
    application.state.dream_service = service
    application.state.closed_loop = closed_loop
    application.state.internship_source_sync = source_sync

    def publication_payload(value: PublicationVersion) -> dict[str, object]:
        payload = asdict(value)
        payload["status"] = value.status.value
        payload["source_event_ids"] = list(value.source_event_ids)
        return payload

    def transition_error(exc: Exception) -> HTTPException:
        return HTTPException(status_code=409, detail=str(exc))

    @application.post("/v1/tasks/start")
    def start_task(scope: ScopeRequest) -> dict[str, object]:
        try:
            if settings.validation_require_active_writeback:
                closed_loop.assert_task_can_start(scope.to_ids())
            return service.start_context(scope.to_ids())
        except TaskStartBlocked as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "latest_completed_event_id": exc.latest_event_id,
                    "active_processed_through_event_id": exc.active_event_id,
                    "next_action": (
                        "complete and activate the pending dream publication"
                    ),
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/v1/dream/conversations", status_code=status.HTTP_202_ACCEPTED)
    def ingest_conversation(payload: ConversationRequest) -> dict[str, object]:
        try:
            service.ingest_conversation(payload.to_event())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"event_id": payload.event_id, "status": "queued"}

    @application.post("/v1/validation/import")
    async def import_manual_ndjson(request: Request) -> dict[str, int]:
        try:
            text = (await request.body()).decode("utf-8")
            return service.import_manual_ndjson(text)
        except (ManualSourceError, UnicodeDecodeError) as exc:
            raise HTTPException(
                status_code=422,
                detail="invalid manual completed-task NDJSON",
            ) from exc

    @application.post("/v1/validation/dream")
    def run_validation_dream(scope: ScopeRequest) -> dict[str, object]:
        try:
            return publication_payload(closed_loop.dream(scope.to_ids()))
        except ClosedLoopError as exc:
            raise HTTPException(
                status_code=503,
                detail="dream candidate generation failed; previous state restored",
            ) from exc
        except ValueError as exc:
            raise transition_error(exc) from exc

    @application.post("/v1/validation/publications/{version}/approve")
    def approve_publication(version: int, scope: ScopeRequest) -> dict[str, object]:
        try:
            return publication_payload(closed_loop.approve(scope.to_ids(), version))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PublicationTransitionError as exc:
            raise transition_error(exc) from exc

    @application.post("/v1/validation/publications/{version}/confirm-writeback")
    def confirm_publication_writeback(
        version: int, payload: WritebackConfirmationRequest
    ) -> dict[str, object]:
        try:
            confirmed = closed_loop.confirm_writeback(
                payload.to_ids(),
                version,
                character_written=payload.character_definition_written,
                user_written=payload.user_persona_written,
            )
            return publication_payload(confirmed)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PublicationTransitionError as exc:
            raise transition_error(exc) from exc

    @application.post("/v1/validation/publications/{version}/activate")
    def activate_publication(version: int, scope: ScopeRequest) -> dict[str, object]:
        try:
            return publication_payload(closed_loop.activate(scope.to_ids(), version))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PublicationTransitionError as exc:
            raise transition_error(exc) from exc

    @application.post("/v1/validation/publications/{version}/reject")
    def reject_publication(version: int, scope: ScopeRequest) -> dict[str, object]:
        try:
            return publication_payload(closed_loop.reject(scope.to_ids(), version))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PublicationTransitionError, ValueError) as exc:
            raise transition_error(exc) from exc

    @application.post("/v1/validation/publications/{version}/rollback")
    def rollback_publication(version: int, scope: ScopeRequest) -> dict[str, object]:
        try:
            return publication_payload(closed_loop.rollback(scope.to_ids(), version))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PublicationTransitionError, ValueError) as exc:
            raise transition_error(exc) from exc

    @application.get("/v1/validation/publications/status")
    def publication_status(
        tenant_id: str, agent_id: str, user_id: str
    ) -> dict[str, object]:
        values = closed_loop.status(ScopeIds(tenant_id, agent_id, user_id))
        return {
            name: publication_payload(value) if value is not None else None
            for name, value in values.items()
        }

    @application.post("/v1/dream/run-pending")
    def run_pending() -> dict[str, object]:
        return {
            "runs": [publication_payload(value) for value in closed_loop.run_pending()]
        }

    @application.post("/v1/dream/run-curators")
    def run_curators(scope: ScopeRequest) -> dict[str, object]:
        reports = service.run_curators(scope.to_ids())
        return {
            name: {
                "run_id": report.run_id,
                "status": report.status,
                "changed": report.changed,
                "archived": report.archived,
                "rollback_snapshot_id": report.rollback_snapshot_id,
            }
            for name, report in reports.items()
        }

    @application.post("/v1/dream/run-due-curators")
    def run_due_curators() -> dict[str, object]:
        results = service.run_due_curators(datetime.now(timezone.utc))
        return {
            "scopes": {
                scope_key: {
                    name: {
                        "run_id": report.run_id,
                        "status": report.status,
                        "changed": report.changed,
                        "archived": report.archived,
                    }
                    for name, report in reports.items()
                }
                for scope_key, reports in results.items()
            }
        }

    @application.post("/v1/dream/rollback/{snapshot_id}")
    def rollback(snapshot_id: str, scope: ScopeRequest) -> dict[str, str]:
        try:
            service.rollback(scope.to_ids(), snapshot_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"snapshot_id": snapshot_id, "status": "restored"}

    @application.get("/v1/dream/reports/{run_id}")
    def read_report(
        run_id: str, tenant_id: str, agent_id: str, user_id: str
    ) -> Response:
        report = service.read_report(ScopeIds(tenant_id, agent_id, user_id), run_id)
        if not report:
            raise HTTPException(status_code=404, detail="report not found")
        return Response(content=report, media_type="application/json")

    return application


_default_env_file = Path(os.environ.get("DREAM_ENV_FILE", ".env")).expanduser()
_default_settings = load_settings(_default_env_file)
app = create_app(Path(_default_settings.home).expanduser(), env_file=_default_env_file)
