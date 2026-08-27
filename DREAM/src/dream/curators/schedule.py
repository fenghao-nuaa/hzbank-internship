"""Persistent fingerprints and daily fallback state for deterministic Curators."""

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from dream.memory.artifacts import AtomicArtifactStore
from dream.core.scope import ScopePaths


class CuratorScheduleStore:
    def __init__(self, paths: ScopePaths) -> None:
        self.paths = paths
        self.artifacts = AtomicArtifactStore(paths.agent_root)
        self.relative = Path("curator-state") / "schedule.json"

    def _read(self) -> dict[str, object]:
        raw = self.artifacts.read_text(self.relative)
        if not raw:
            return {}
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("curator schedule state must be an object")
        return value

    def _write(self, state: dict[str, object]) -> None:
        self.artifacts.write_text(
            self.relative,
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def ai_fingerprint(self) -> str:
        digest = hashlib.sha256()
        for card in sorted(self.paths.decision_cards_dir.glob("*.md")):
            if card.is_file():
                digest.update(card.name.encode("utf-8"))
                digest.update(b"\0")
                digest.update(card.read_bytes())
                digest.update(b"\0")
        return digest.hexdigest()

    def user_fingerprint(self, user_id: str) -> str:
        content = self.artifacts.read_text(Path("users") / user_id / "USER.md")
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def ai_has_content(self) -> bool:
        return any(self.paths.decision_cards_dir.glob("*.md"))

    def user_has_content(self, user_id: str) -> bool:
        return bool(
            self.artifacts.read_text(Path("users") / user_id / "USER.md").strip()
        )

    def ai_changed(self) -> bool:
        state = self._read()
        ai = state.get("ai")
        if not isinstance(ai, dict):
            return self.ai_has_content()
        return ai.get("fingerprint") != self.ai_fingerprint()

    def user_changed(self, user_id: str) -> bool:
        state = self._read()
        users = state.get("users")
        user = users.get(user_id) if isinstance(users, dict) else None
        if not isinstance(user, dict):
            return self.user_has_content(user_id)
        return user.get("fingerprint") != self.user_fingerprint(user_id)

    def record_ai_success(self, now: datetime) -> None:
        state = self._read()
        state["ai"] = {
            "fingerprint": self.ai_fingerprint(),
            "last_success_at": now.isoformat(),
        }
        self._write(state)

    def record_user_success(self, user_id: str, now: datetime) -> None:
        state = self._read()
        users = state.setdefault("users", {})
        if not isinstance(users, dict):
            users = {}
            state["users"] = users
        users[user_id] = {
            "fingerprint": self.user_fingerprint(user_id),
            "last_success_at": now.isoformat(),
        }
        self._write(state)

    def daily_due(self, now: datetime, timezone: ZoneInfo, hour: int) -> bool:
        local = now.astimezone(timezone)
        if local.hour < hour:
            return False
        state = self._read()
        daily = state.get("daily")
        checked_date = (
            daily.get("last_checked_date") if isinstance(daily, dict) else None
        )
        return checked_date != local.date().isoformat()

    def record_daily_check(
        self,
        now: datetime,
        timezone: ZoneInfo,
        *,
        processed_scopes: tuple[str, ...],
        errors: tuple[str, ...],
    ) -> None:
        state = self._read()
        local = now.astimezone(timezone)
        daily: dict[str, object] = {
            "last_checked_date": local.date().isoformat(),
            "last_checked_at": now.isoformat(),
            "processed_scopes": list(processed_scopes),
            "errors": list(errors),
        }
        if not errors:
            daily["last_success_at"] = now.isoformat()
        state["daily"] = daily
        self._write(state)

    def semantic_due(self, now: datetime, interval: timedelta) -> bool:
        state = self._read()
        semantic = state.get("semantic")
        if not isinstance(semantic, dict):
            return True
        raw = semantic.get("last_attempt_at")
        if not isinstance(raw, str) or not raw:
            return True
        last_attempt = datetime.fromisoformat(raw)
        return now - last_attempt >= interval

    def record_semantic_attempt(
        self,
        now: datetime,
        *,
        run_id: str,
        status: str,
        candidate_path: str,
        error: str,
    ) -> None:
        state = self._read()
        semantic: dict[str, object] = {
            "last_attempt_at": now.isoformat(),
            "run_id": run_id,
            "status": status,
            "candidate_path": candidate_path,
            "error": error,
        }
        if status == "candidate_ready":
            semantic["last_success_at"] = now.isoformat()
        state["semantic"] = semantic
        self._write(state)
