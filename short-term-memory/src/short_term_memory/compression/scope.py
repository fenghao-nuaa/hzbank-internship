"""Opaque per-session scope for external context optimizers."""

from dataclasses import dataclass
import hashlib
import hmac

from short_term_memory.compression.telemetry import HeadroomTelemetry


@dataclass(frozen=True)
class OptimizationScope:
    user_scope: str
    session_scope: str
    workspace_scope: str

    def as_headroom_headers(self) -> dict[str, str]:
        return {
            "x-headroom-user-id": self.user_scope,
            "x-headroom-session-id": self.session_scope,
            "x-headroom-project-id": self.workspace_scope,
        }


class OptimizationScopeFactory:
    def __init__(
        self,
        secret: str,
        *,
        telemetry: HeadroomTelemetry | None = None,
    ) -> None:
        if not secret.strip():
            raise ValueError("scope secret must not be blank")
        self._secret = secret.encode("utf-8")
        self._telemetry = telemetry

    def for_session(self, user_id: str, session_id: str) -> OptimizationScope:
        if not user_id or not session_id:
            raise ValueError("user_id and session_id must not be blank")
        try:
            session_value = f"{user_id}:{session_id}"
            return OptimizationScope(
                user_scope=self._digest("user", user_id),
                session_scope=self._digest("session", session_value),
                workspace_scope=self._digest("workspace", session_value),
            )
        except Exception:
            if self._telemetry is not None:
                self._telemetry.record_scope_generation_failure()
            raise

    def _digest(self, label: str, value: str) -> str:
        digest = hmac.new(
            self._secret,
            f"dream-v1:{label}:{value}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"dream-v1-{digest}"
