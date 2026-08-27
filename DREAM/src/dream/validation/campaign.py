"""Append-only state and hard phase gates for the Codex validation campaign."""

from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


VALIDATION_USERS = (
    "project-manager",
    "python-beginner",
    "technical-lead",
)
_UserId = Literal["project-manager", "python-beginner", "technical-lead"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
EMPTY_CONTEXT_SHA256 = hashlib.sha256(b"").hexdigest()


class CampaignBlocked(RuntimeError):
    """The next task cannot start until a required campaign condition is met."""


class CampaignValidationError(ValueError):
    """Campaign state or a receipt violates the validation contract."""


class CampaignPhase(StrEnum):
    BASELINE = "baseline"
    EVOLVED_V1 = "evolved_v1"
    EVOLVED_V2 = "evolved_v2"


class CodexTaskReceipt(BaseModel):
    """Immutable evidence that one fresh Codex task produced one Agent answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: _UserId
    task_number: int = Field(ge=1, le=12)
    event_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dream_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_publication_version: int = Field(ge=0)

    @model_validator(mode="after")
    def phase_fields_are_consistent(self) -> "CodexTaskReceipt":
        if not self.event_id.strip() or not self.thread_id.strip():
            raise ValueError("receipt identifiers must not be blank")
        if self.phase is CampaignPhase.BASELINE:
            if self.active_publication_version != 0:
                raise ValueError("baseline receipt must use publication version 0")
            if self.dream_context_sha256 != EMPTY_CONTEXT_SHA256:
                raise ValueError("baseline receipt must use the empty DREAM context")
        else:
            if self.active_publication_version == 0:
                raise ValueError("evolved receipt requires an active publication")
            if self.dream_context_sha256 == EMPTY_CONTEXT_SHA256:
                raise ValueError("evolved receipt requires non-empty DREAM context")
        return self

    @property
    def phase(self) -> CampaignPhase:
        if self.task_number <= 5:
            return CampaignPhase.BASELINE
        if self.task_number <= 10:
            return CampaignPhase.EVOLVED_V1
        return CampaignPhase.EVOLVED_V2


class CodexCampaignStore:
    """Persist receipts and prevent phase advancement before DREAM publication."""

    def __init__(self, root: Path, *, approved_profile_sha: str) -> None:
        if not _SHA256.fullmatch(approved_profile_sha):
            raise CampaignValidationError("approved profile hash must be SHA-256")
        self.approved_profile_sha = approved_profile_sha
        self.root = Path(root) / "campaign"
        self.users_root = self.root / "users"
        self.receipts_path = self.root / "receipts.jsonl"
        self.users_root.mkdir(parents=True, exist_ok=True)
        self._receipts: dict[str, list[CodexTaskReceipt]] = {
            user_id: [] for user_id in VALIDATION_USERS
        }
        self._cycles: dict[str, dict[int, int]] = {
            user_id: {} for user_id in VALIDATION_USERS
        }
        self._thread_ids: set[str] = set()
        self._event_ids: set[str] = set()
        self._load_cycles()
        self._load_receipts()

    def assert_can_create(self, user_id: str, task_number: int) -> None:
        self._require_user(user_id)
        if not 1 <= task_number <= 12:
            raise CampaignBlocked("task number must be between 1 and 12")
        expected = len(self._receipts[user_id]) + 1
        if task_number != expected:
            raise CampaignBlocked(
                f"expected task {expected} for {user_id}, got task {task_number}"
            )
        if task_number == 6 and 1 not in self._cycles[user_id]:
            raise CampaignBlocked("dream cycle 1 must be active before task 6")
        if task_number == 11 and 2 not in self._cycles[user_id]:
            raise CampaignBlocked("dream cycle 2 must be active before task 11")

    def note_active_cycle(self, user_id: str, *, cycle: int, version: int) -> None:
        self._require_user(user_id)
        if cycle not in (1, 2):
            raise CampaignValidationError("cycle must be 1 or 2")
        if version < 1:
            raise CampaignValidationError("active publication version must be positive")
        minimum_tasks = 5 if cycle == 1 else 10
        if len(self._receipts[user_id]) < minimum_tasks:
            raise CampaignBlocked(
                f"dream cycle {cycle} requires {minimum_tasks} completed tasks"
            )
        expected_cycle = len(self._cycles[user_id]) + 1
        if cycle != expected_cycle:
            raise CampaignValidationError(
                f"expected dream cycle {expected_cycle}, got cycle {cycle}"
            )
        if cycle == 2 and version <= self._cycles[user_id][1]:
            raise CampaignValidationError(
                "cycle 2 publication version must be newer than cycle 1"
            )
        self._cycles[user_id][cycle] = version
        self._write_user_state(user_id)

    def record(self, receipt: CodexTaskReceipt) -> None:
        if receipt.profile_sha256 != self.approved_profile_sha:
            raise CampaignValidationError("receipt profile hash does not match approval")
        if receipt.thread_id in self._thread_ids:
            raise CampaignValidationError("duplicate Codex thread ID")
        if receipt.event_id in self._event_ids:
            raise CampaignValidationError("duplicate event ID")
        self.assert_can_create(receipt.user_id, receipt.task_number)
        expected_version = self._expected_version(receipt)
        if receipt.active_publication_version != expected_version:
            raise CampaignValidationError(
                "receipt publication version is not the active dream version"
            )
        self._append_receipt(receipt)
        self._remember(receipt)
        self._write_user_state(receipt.user_id)

    def summary(self) -> dict[str, object]:
        users = {
            user_id: {
                "task_count": len(self._receipts[user_id]),
                "active_cycles": len(self._cycles[user_id]),
            }
            for user_id in VALIDATION_USERS
        }
        if any(
            entry["task_count"] != 12 or entry["active_cycles"] != 2
            for entry in users.values()
        ):
            raise CampaignBlocked("validation campaign is incomplete")
        return {
            "task_count": sum(entry["task_count"] for entry in users.values()),
            "unique_thread_count": len(self._thread_ids),
            "unique_event_count": len(self._event_ids),
            "profile_sha256": self.approved_profile_sha,
            "users": users,
        }

    def _expected_version(self, receipt: CodexTaskReceipt) -> int:
        if receipt.phase is CampaignPhase.BASELINE:
            return 0
        cycle = 1 if receipt.phase is CampaignPhase.EVOLVED_V1 else 2
        try:
            return self._cycles[receipt.user_id][cycle]
        except KeyError as exc:
            raise CampaignBlocked(
                f"dream cycle {cycle} is not active for {receipt.user_id}"
            ) from exc

    def _append_receipt(self, receipt: CodexTaskReceipt) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(receipt.model_dump(), ensure_ascii=False) + "\n"
        with self.receipts_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _remember(self, receipt: CodexTaskReceipt) -> None:
        self._receipts[receipt.user_id].append(receipt)
        self._thread_ids.add(receipt.thread_id)
        self._event_ids.add(receipt.event_id)

    def _load_receipts(self) -> None:
        if not self.receipts_path.exists():
            return
        try:
            lines = self.receipts_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise CampaignValidationError("campaign receipts cannot be read") from exc
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                receipt = CodexTaskReceipt.model_validate_json(line)
                if receipt.profile_sha256 != self.approved_profile_sha:
                    raise CampaignValidationError("stored profile hash changed")
                if receipt.thread_id in self._thread_ids:
                    raise CampaignValidationError("duplicate stored thread ID")
                if receipt.event_id in self._event_ids:
                    raise CampaignValidationError("duplicate stored event ID")
                expected = len(self._receipts[receipt.user_id]) + 1
                if receipt.task_number != expected:
                    raise CampaignValidationError("stored task sequence is invalid")
                if receipt.active_publication_version != self._expected_version(receipt):
                    raise CampaignValidationError("stored publication version is invalid")
            except Exception as exc:
                if isinstance(exc, CampaignValidationError):
                    raise
                raise CampaignValidationError(
                    f"invalid campaign receipt at line {line_number}"
                ) from exc
            self._remember(receipt)

    def _load_cycles(self) -> None:
        for user_id in VALIDATION_USERS:
            path = self.users_root / f"{user_id}.json"
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data["user_id"] != user_id:
                    raise ValueError("wrong user")
                raw_cycles = data["active_cycles"]
                cycles = {int(key): int(value) for key, value in raw_cycles.items()}
                if set(cycles) not in (set(), {1}, {1, 2}):
                    raise ValueError("invalid cycle sequence")
                if any(version < 1 for version in cycles.values()):
                    raise ValueError("invalid version")
                if 2 in cycles and cycles[2] <= cycles[1]:
                    raise ValueError("non-increasing version")
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CampaignValidationError(
                    f"campaign user state is invalid for {user_id}"
                ) from exc
            self._cycles[user_id] = cycles

    def _write_user_state(self, user_id: str) -> None:
        payload = {
            "user_id": user_id,
            "task_count": len(self._receipts[user_id]),
            "active_cycles": {
                str(cycle): version
                for cycle, version in sorted(self._cycles[user_id].items())
            },
        }
        path = self.users_root / f"{user_id}.json"
        temporary = path.with_name(f".{path.name}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _require_user(user_id: str) -> None:
        if user_id not in VALIDATION_USERS:
            raise CampaignValidationError("unknown validation user")
