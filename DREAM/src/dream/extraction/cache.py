"""Durable cache for validated semantic Background Review results."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from dream.memory.artifacts import AtomicArtifactStore
from dream.core.events import TaskCompletedEvent
from dream.extraction.models import (
    ArtifactKind,
    ReviewAction,
    ReviewEventDisposition,
    ReviewResult,
)
from dream.core.scope import ScopeIds
from dream.memory.storage.snapshots import ContextSnapshot


_CACHE_FORMAT = 2


@dataclass(frozen=True)
class ReviewCacheKey:
    input_hash: str
    backend_identity: dict[str, object]


class ReviewStageCache:
    """Cache only successful, already locally validated semantic results."""

    def __init__(self, home: Path) -> None:
        self.artifacts = AtomicArtifactStore(home)

    def key_for(
        self,
        ids: ScopeIds,
        events: tuple[TaskCompletedEvent, ...],
        allowed_tools_by_event: dict[str, frozenset[str]],
        snapshot: ContextSnapshot,
        backend: object,
    ) -> ReviewCacheKey | None:
        if not bool(getattr(backend, "validated_semantic_cache", False)):
            return None
        model = getattr(backend, "model", "")
        if not isinstance(model, str) or not model:
            return None
        identity = {
            "backend": f"{type(backend).__module__}.{type(backend).__qualname__}",
            "model": model,
            "structured_mode": str(getattr(backend, "structured_mode", "")),
            "prompt_version": str(
                getattr(backend, "prompt_version", "combined-review-v2")
            ),
            "max_completion_tokens": getattr(backend, "max_completion_tokens", None),
        }
        payload = {
            "format": _CACHE_FORMAT,
            "scope": {
                "tenant_id": ids.tenant_id,
                "agent_id": ids.agent_id,
                "user_id": ids.user_id,
            },
            "events": [
                {
                    "event_id": event.event_id,
                    "task_id": event.task_id,
                    "completed_at": event.completed_at,
                    "interrupted": event.interrupted,
                    "tool_iterations": event.tool_iterations,
                    "transcript": event.transcript,
                    "final_response": event.final_response,
                    "source_refs": event.source_refs,
                    "allowed_tools": sorted(allowed_tools_by_event[event.event_id]),
                }
                for event in events
            ],
            "snapshot_id": snapshot.snapshot_id,
            "backend": identity,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return ReviewCacheKey(
            input_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            backend_identity=identity,
        )

    def load(self, key: ReviewCacheKey) -> ReviewResult | None:
        raw = self.artifacts.read_text(self._relative(key.input_hash))
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            if (
                payload.get("format") != _CACHE_FORMAT
                or payload.get("input_hash") != key.input_hash
                or payload.get("backend") != key.backend_identity
            ):
                return None
            encoded_result = payload["result"]
            if payload.get("result_sha256") != self._result_hash(encoded_result):
                return None
            result = self._decode_result(encoded_result)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return result if result.status == "success" else None

    def store(self, key: ReviewCacheKey, result: ReviewResult) -> None:
        if result.status != "success" or result.error:
            return
        encoded_result = self._encode_result(result)
        payload = {
            "format": _CACHE_FORMAT,
            "input_hash": key.input_hash,
            "backend": key.backend_identity,
            "result": encoded_result,
            "result_sha256": self._result_hash(encoded_result),
        }
        self.artifacts.write_text(
            self._relative(key.input_hash),
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    @staticmethod
    def _relative(input_hash: str) -> Path:
        return Path("review-cache") / f"{input_hash}.json"

    @staticmethod
    def _result_hash(result: object) -> str:
        canonical = json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _encode_result(result: ReviewResult) -> dict[str, object]:
        return {
            "status": result.status,
            "summary": result.summary,
            "error": result.error,
            "actions": [
                {
                    "kind": action.kind.value,
                    "tool_name": action.tool_name,
                    "payload": action.payload,
                    "source_event_id": action.source_event_id,
                    "source_event_ids": list(action.source_event_ids),
                }
                for action in result.actions
            ],
            "event_dispositions": [
                {
                    "event_id": disposition.event_id,
                    "disposition": disposition.disposition,
                    "reason": disposition.reason,
                }
                for disposition in result.event_dispositions
            ],
            "trace": result.trace,
        }

    @staticmethod
    def _decode_result(payload: object) -> ReviewResult:
        if not isinstance(payload, dict):
            raise TypeError("cached result must be an object")
        raw_actions = payload.get("actions")
        if not isinstance(raw_actions, list):
            raise TypeError("cached actions must be a list")
        raw_dispositions = payload.get("event_dispositions")
        if not isinstance(raw_dispositions, list):
            raise TypeError("cached event dispositions must be a list")
        actions: list[ReviewAction] = []
        for raw in raw_actions:
            if not isinstance(raw, dict) or not isinstance(raw.get("payload"), dict):
                raise TypeError("cached action is invalid")
            sources = raw.get("source_event_ids", [])
            if not isinstance(sources, list) or not all(
                isinstance(item, str) for item in sources
            ):
                raise TypeError("cached action sources are invalid")
            actions.append(
                ReviewAction(
                    kind=ArtifactKind(str(raw["kind"])),
                    tool_name=str(raw["tool_name"]),
                    payload=dict(raw["payload"]),
                    source_event_id=str(raw["source_event_id"]),
                    source_event_ids=tuple(sources),
                )
            )
        dispositions: list[ReviewEventDisposition] = []
        for raw in raw_dispositions:
            if not isinstance(raw, dict):
                raise TypeError("cached event disposition is invalid")
            event_id = raw.get("event_id")
            disposition = raw.get("disposition")
            reason = raw.get("reason")
            if (
                not isinstance(event_id, str)
                or disposition not in {"used", "no_durable_signal"}
                or (reason is not None and not isinstance(reason, str))
            ):
                raise TypeError("cached event disposition is invalid")
            dispositions.append(
                ReviewEventDisposition(
                    event_id=event_id,
                    disposition=str(disposition),
                    reason=reason,
                )
            )
        status = payload.get("status")
        summary = payload.get("summary")
        error = payload.get("error")
        if not isinstance(status, str) or not isinstance(summary, str):
            raise TypeError("cached result metadata is invalid")
        if error is not None and not isinstance(error, str):
            raise TypeError("cached result error is invalid")
        return ReviewResult(
            actions=tuple(actions),
            summary=summary,
            status=status,
            error=error,
            event_dispositions=tuple(dispositions),
            trace=(
                dict(payload["trace"])
                if isinstance(payload.get("trace"), dict)
                else None
            ),
        )
