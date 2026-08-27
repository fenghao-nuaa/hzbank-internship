"""Atomic async Redis persistence for original memory events and summaries."""

from dataclasses import dataclass
import json
from typing import Any, Literal, Protocol

from short_term_memory.models import (
    EventReservation,
    MemoryEvent,
    MemorySummaryEnvelope,
    migrate_v1_envelope,
)
from short_term_memory.storage.recent_originals import select_recent_turns
from short_term_memory.storage.vfs_adapter import safe_component


class EventConflictError(ValueError):
    """Raised when an idempotency key is reused with a different digest."""


class AsyncRedisClient(Protocol):
    async def eval(self, script: str, numkeys: int, *args: str) -> Any: ...

    async def lrange(self, key: str, start: int, end: int) -> list[Any]: ...

    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: str, **kwargs: Any) -> Any: ...


RESERVE_EVENT_SCRIPT = """
-- dream:reserve-event
local digest = redis.call('HGET', KEYS[2], 'digest')
if digest then
  if digest ~= ARGV[1] then return {'conflict', '0'} end
  return {redis.call('HGET', KEYS[2], 'status'), redis.call('HGET', KEYS[2], 'sequence')}
end
local sequence = redis.call('INCR', KEYS[1])
redis.call('HSET', KEYS[2], 'digest', ARGV[1], 'status', 'pending', 'sequence', sequence)
redis.call('SADD', KEYS[3], ARGV[3])
redis.call('EXPIRE', KEYS[2], ARGV[2])
redis.call('EXPIRE', KEYS[3], ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return {'reserved', tostring(sequence)}
"""

COMMIT_EVENT_SCRIPT = """
-- dream:commit-event
local status = redis.call('HGET', KEYS[4], 'status')
if not status then return {'missing'} end
if redis.call('HGET', KEYS[4], 'sequence') ~= ARGV[2] then return {'sequence_conflict'} end
if redis.call('HGET', KEYS[4], 'digest') ~= ARGV[3] then return {'digest_conflict'} end
if status == 'committed' then return {'duplicate'} end
redis.call('RPUSH', KEYS[2], ARGV[1])
redis.call('HSET', KEYS[4], 'status', 'committed')
redis.call('SREM', KEYS[5], ARGV[5])
if redis.call('SCARD', KEYS[5]) == 0 then redis.call('DEL', KEYS[5]) end
redis.call('EXPIRE', KEYS[2], ARGV[4])
redis.call('EXPIRE', KEYS[3], ARGV[4])
redis.call('EXPIRE', KEYS[4], ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return {'committed'}
"""

RESTORE_ORIGINALS_SCRIPT = """
-- dream:restore-originals-v1
local originals = cjson.decode(ARGV[1])
local event_prefix = ARGV[2]
local ttl = ARGV[3]
local maximum = tonumber(ARGV[4])
for _, event in ipairs(originals) do
  local key = event_prefix .. event.event_id
  local digest = redis.call('HGET', key, 'digest')
  if digest and (digest ~= event.sha256
      or redis.call('HGET', key, 'sequence') ~= tostring(event.sequence)) then
    return {'conflict'}
  end
end
if redis.call('LLEN', KEYS[2]) > 0 then return {'not_restored'} end
if redis.call('EXISTS', KEYS[1]) == 1 or redis.call('SCARD', KEYS[3]) > 0 then
  return {'not_restored'}
end
for _, event in ipairs(originals) do
  local key = event_prefix .. event.event_id
  redis.call('HSET', key, 'digest', event.sha256, 'status', 'committed',
    'sequence', tostring(event.sequence))
  redis.call('EXPIRE', key, ttl)
  redis.call('RPUSH', KEYS[2], cjson.encode(event))
end
redis.call('SET', KEYS[1], tostring(maximum), 'EX', ttl)
redis.call('EXPIRE', KEYS[2], ttl)
return {'restored'}
"""

RESTORE_SESSION_PROJECTION_SCRIPT = """
-- dream:restore-session-projection-v1
if redis.call('EXISTS', KEYS[1]) == 1
  or redis.call('LLEN', KEYS[2]) > 0
  or redis.call('EXISTS', KEYS[3]) == 1
  or redis.call('SCARD', KEYS[4]) > 0 then
  return {'not_restored'}
end
local originals = cjson.decode(ARGV[1])
for _, event in ipairs(originals) do
  local event_key = ARGV[2] .. event.event_id
  redis.call('HSET', event_key, 'digest', event.sha256, 'status', 'committed',
    'sequence', tostring(event.sequence))
  redis.call('EXPIRE', event_key, ARGV[3])
  redis.call('RPUSH', KEYS[2], cjson.encode(event))
end
redis.call('SET', KEYS[1], ARGV[4], 'EX', ARGV[3])
if #originals > 0 then redis.call('EXPIRE', KEYS[2], ARGV[3]) end
if ARGV[5] ~= '' then
  redis.call('SET', KEYS[3], ARGV[5], 'EX', ARGV[3])
end
return {'restored'}
"""

CAS_ENVELOPE_SCRIPT = """
-- dream:compare-and-set-envelope
local current = redis.call('GET', KEYS[1])
if current then
  local parsed = cjson.decode(current)
  if tostring(parsed.version) ~= ARGV[1] then return {'0'} end
elseif ARGV[1] ~= '0' then
  return {'0'}
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return {'1'}
"""

RELEASE_LEASE_SCRIPT = """
-- dream:release-compression-lease
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return {'0'} end
redis.call('DEL', KEYS[1])
return {'1'}
"""

RELEASE_SESSION_MEMORY_EXTRACTION_SCRIPT = """
-- dream:release-session-memory-extraction
local current = redis.call('GET', KEYS[1])
if not current then return {'0'} end
local parsed = cjson.decode(current)
if parsed.token ~= ARGV[1] then return {'0'} end
redis.call('DEL', KEYS[1])
return {'1'}
"""


@dataclass(frozen=True)
class SessionMemoryExtractionState:
    token: str
    expected_version: int
    started_at: str

TRIM_ORIGINALS_SCRIPT = """
-- dream:trim-originals-v3
-- Remove original messages whose sequence <= ARGV[1] (compressed_through),
-- EXCEPT the most recent messages that fit within ARGV[3] (retain_budget in
-- approximate chars).  Newer originals stay so read returns them directly.
local through = tonumber(ARGV[1])
if not through or through < 0 then return {'0'} end
local ttl = ARGV[2]
local retain_budget = tonumber(ARGV[3] or 0)
if retain_budget < 0 then retain_budget = 0 end
local all = redis.call('LRANGE', KEYS[1], 0, -1)
local kept = {}
local remaining = 0
-- Walk newest -> oldest, keeping originals until the budget fills.
local budget_left = retain_budget
local skip = 0
for i = #all, 1, -1 do
  local event = cjson.decode(all[i])
  if tonumber(event.sequence) > through then
    -- Newer-than-compressed always kept.
  else
    if budget_left > 0 then
      budget_left = budget_left - #(event.content or '')
      if budget_left < 0 then
        skip = i
        break
      end
    else
      skip = i
      break
    end
  end
end
for i = skip + 1, #all do
  local event = cjson.decode(all[i])
  if tonumber(event.sequence) > through or i > skip then
    kept[#kept + 1] = all[i]
    remaining = remaining + 1
  end
end
if remaining == 0 then
  redis.call('DEL', KEYS[1])
else
  redis.call('DEL', KEYS[1])
  if #kept > 0 then
    redis.call('RPUSH', KEYS[1], unpack(kept))
    redis.call('EXPIRE', KEYS[1], ttl)
  end
end
return {tostring(remaining)}
"""


class AsyncRedisMemoryStore:
    def __init__(self, client: AsyncRedisClient, *, ttl_seconds: int = 43_200) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self.client = client
        self.ttl_seconds = ttl_seconds

    async def reserve_event(
        self, user_id: str, session_id: str, event_id: str, digest: str
    ) -> EventReservation:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("digest must be a SHA-256 hex digest")
        keys = self._keys(user_id, session_id, event_id)
        result = await self.client.eval(
            RESERVE_EVENT_SCRIPT,
            3,
            keys.sequence,
            keys.event,
            keys.pending_reservations,
            digest,
            str(self.ttl_seconds),
            event_id,
        )
        state, sequence = self._result(result)
        if state == "conflict":
            raise EventConflictError("event_id is already reserved for another digest")
        return EventReservation(sequence=int(sequence), state=state)

    async def commit_event(
        self, user_id: str, session_id: str, event: MemoryEvent
    ) -> Literal["committed", "duplicate"]:
        keys = self._keys(user_id, session_id, event.event_id)
        result = await self.client.eval(
            COMMIT_EVENT_SCRIPT,
            5,
            keys.sequence,
            keys.messages,
            keys.summary,
            keys.event,
            keys.pending_reservations,
            event.model_dump_json(),
            str(event.sequence),
            event.sha256,
            str(self.ttl_seconds),
            event.event_id,
        )
        status = self._result(result)[0]
        if status in {"committed", "duplicate"}:
            return status
        if status == "missing":
            raise ValueError("event must be reserved before it is committed")
        if status == "digest_conflict":
            raise EventConflictError("event digest does not match its reservation")
        raise ValueError("event sequence does not match its reservation")

    async def restore_originals(
        self,
        user_id: str,
        session_id: str,
        originals: tuple[MemoryEvent, ...],
    ) -> bool:
        """Atomically restore a bounded journal tail without renumbering it."""

        if not originals:
            return False
        ordered = tuple(sorted(originals, key=lambda event: event.sequence))
        if ordered != originals or len({event.event_id for event in originals}) != len(originals):
            raise ValueError("originals must have ordered unique event IDs")
        if len({event.sequence for event in originals}) != len(originals):
            raise ValueError("originals must have unique sequences")
        keys = self._keys(user_id, session_id)
        result = await self.client.eval(
            RESTORE_ORIGINALS_SCRIPT,
            3,
            keys.sequence,
            keys.messages,
            keys.pending_reservations,
            json.dumps(
                [event.model_dump(mode="json") for event in originals],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            f"{keys.sequence.rsplit(':sequence', 1)[0]}:event:",
            str(self.ttl_seconds),
            str(originals[-1].sequence),
        )
        state = self._result(result)[0]
        if state == "restored":
            return True
        if state == "not_restored":
            return False
        if state == "conflict":
            raise EventConflictError("journal originals conflict with Redis reservation")
        raise ValueError("unexpected restore result")

    async def restore_session_projection(
        self,
        user_id: str,
        session_id: str,
        *,
        latest_sequence: int,
        originals: tuple[MemoryEvent, ...],
        envelope: MemorySummaryEnvelope | None,
    ) -> bool:
        """Atomically restore a cold session's bounded Redis projection."""

        if latest_sequence < 0:
            raise ValueError("latest_sequence must not be negative")
        ordered = tuple(sorted(originals, key=lambda event: event.sequence))
        if ordered != originals or len({event.event_id for event in originals}) != len(
            originals
        ):
            raise ValueError("originals must have ordered unique event IDs")
        if len({event.sequence for event in originals}) != len(originals):
            raise ValueError("originals must have unique sequences")
        if originals and latest_sequence < originals[-1].sequence:
            raise ValueError("latest_sequence must cover all restored originals")

        keys = self._keys(user_id, session_id)
        result = await self.client.eval(
            RESTORE_SESSION_PROJECTION_SCRIPT,
            4,
            keys.sequence,
            keys.messages,
            keys.summary,
            keys.pending_reservations,
            json.dumps(
                [event.model_dump(mode="json") for event in originals],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            f"{keys.sequence.rsplit(':sequence', 1)[0]}:event:",
            str(self.ttl_seconds),
            str(latest_sequence),
            envelope.model_dump_json() if envelope is not None else "",
        )
        state = self._result(result)[0]
        if state == "restored":
            return True
        if state == "not_restored":
            return False
        raise ValueError("unexpected session projection restore result")

    async def read_recent_originals(
        self, user_id: str, session_id: str, history_turns: int
    ) -> tuple[MemoryEvent, ...]:
        if history_turns < 1:
            raise ValueError("history_turns must be positive")
        keys = self._keys(user_id, session_id)
        return select_recent_turns(
            self._events(await self.client.lrange(keys.messages, 0, -1)), history_turns
        )

    async def read_latest_sequence(self, user_id: str, session_id: str) -> int:
        value = await self.client.get(self._keys(user_id, session_id).sequence)
        return 0 if value is None else int(self._text(value))

    async def read_originals_after(
        self, user_id: str, session_id: str, sequence: int
    ) -> tuple[MemoryEvent, ...]:
        if sequence < 0:
            raise ValueError("sequence must not be negative")
        keys = self._keys(user_id, session_id)
        return tuple(
            event
            for event in self._events(await self.client.lrange(keys.messages, 0, -1))
            if event.sequence > sequence
        )

    async def read_envelope(
        self, user_id: str, session_id: str
    ) -> MemorySummaryEnvelope | None:
        value = await self.client.get(self._keys(user_id, session_id).summary)
        if value is None:
            return None
        return migrate_v1_envelope(json.loads(self._text(value)))

    async def compare_and_set_envelope(
        self,
        user_id: str,
        session_id: str,
        expected_version: int,
        envelope: MemorySummaryEnvelope,
    ) -> bool:
        if expected_version < 0:
            raise ValueError("expected_version must not be negative")
        result = await self.client.eval(
            CAS_ENVELOPE_SCRIPT,
            1,
            self._keys(user_id, session_id).summary,
            str(expected_version),
            envelope.model_dump_json(),
            str(self.ttl_seconds),
        )
        return self._result(result)[0] == "1"

    async def trim_originals(
        self,
        user_id: str,
        session_id: str,
        through_sequence: int,
        retain_budget: int = 0,
    ) -> int:
        """Remove compressed original messages (sequence <= through) from Redis.

        The most recent originals that fit within ``retain_budget`` (approximate
        chars) are kept as original text so read can return them directly; older
        compressed originals are trimmed.  The journal still owns the durable
        original.  Returns the number of messages remaining in the list.
        """
        if through_sequence < 0:
            raise ValueError("through_sequence must not be negative")
        if retain_budget < 0:
            raise ValueError("retain_budget must not be negative")
        keys = self._keys(user_id, session_id)
        result = await self.client.eval(
            TRIM_ORIGINALS_SCRIPT,
            1,
            keys.messages,
            str(through_sequence),
            str(self.ttl_seconds),
            str(retain_budget),
        )
        return int(self._result(result)[0])

    async def store_ccr_summary(
        self, user_id: str, session_id: str, hash_value: str, summary: str
    ) -> None:
        """Record the content summary for a marker hash (hash -> summary)."""
        if not hash_value:
            raise ValueError("hash_value must not be blank")
        keys = self._keys(user_id, session_id)
        await self.client.hset(keys.ccr_summaries, hash_value, summary)
        await self.client.expire(keys.ccr_summaries, self.ttl_seconds)

    async def get_ccr_summaries(
        self, user_id: str, session_id: str
    ) -> dict[str, str]:
        """Return all recorded hash -> content-summary mappings for a session."""
        keys = self._keys(user_id, session_id)
        raw = await self.client.hgetall(keys.ccr_summaries)
        return {self._text(k): self._text(v) for k, v in raw.items()}

    async def acquire_compression_lease(
        self, user_id: str, session_id: str, token: str
    ) -> bool:
        if not token:
            raise ValueError("lease token must not be blank")
        result = await self.client.set(
            self._keys(user_id, session_id).compression_lock,
            token,
            nx=True,
            px=self.ttl_seconds * 1000,
        )
        return bool(result)

    async def release_compression_lease(
        self, user_id: str, session_id: str, token: str
    ) -> bool:
        if not token:
            raise ValueError("lease token must not be blank")
        result = await self.client.eval(
            RELEASE_LEASE_SCRIPT,
            1,
            self._keys(user_id, session_id).compression_lock,
            token,
        )
        return self._result(result)[0] == "1"

    async def acquire_session_memory_extraction(
        self,
        user_id: str,
        session_id: str,
        token: str,
        *,
        expected_version: int,
        started_at: str,
    ) -> bool:
        if not token:
            raise ValueError("extraction token must not be blank")
        if expected_version < 0:
            raise ValueError("expected_version must not be negative")
        if not started_at:
            raise ValueError("started_at must not be blank")
        payload = json.dumps(
            {
                "token": token,
                "expected_version": expected_version,
                "started_at": started_at,
            },
            separators=(",", ":"),
        )
        result = await self.client.set(
            self._keys(user_id, session_id).session_memory_extraction,
            payload,
            nx=True,
            px=60_000,
        )
        return bool(result)

    async def read_session_memory_extraction(
        self, user_id: str, session_id: str
    ) -> SessionMemoryExtractionState | None:
        value = await self.client.get(
            self._keys(user_id, session_id).session_memory_extraction
        )
        if value is None:
            return None
        raw = json.loads(self._text(value))
        return SessionMemoryExtractionState(
            token=str(raw["token"]),
            expected_version=int(raw["expected_version"]),
            started_at=str(raw["started_at"]),
        )

    async def release_session_memory_extraction(
        self, user_id: str, session_id: str, token: str
    ) -> bool:
        if not token:
            raise ValueError("extraction token must not be blank")
        result = await self.client.eval(
            RELEASE_SESSION_MEMORY_EXTRACTION_SCRIPT,
            1,
            self._keys(user_id, session_id).session_memory_extraction,
            token,
        )
        return self._result(result)[0] == "1"

    async def acquire_context_compaction_lease(
        self, user_id: str, session_id: str, token: str
    ) -> bool:
        if not token:
            raise ValueError("context lease token must not be blank")
        result = await self.client.set(
            self._keys(user_id, session_id).context_compaction_lock,
            token,
            nx=True,
            px=300_000,
        )
        return bool(result)

    async def release_context_compaction_lease(
        self, user_id: str, session_id: str, token: str
    ) -> bool:
        if not token:
            raise ValueError("context lease token must not be blank")
        result = await self.client.eval(
            RELEASE_LEASE_SCRIPT,
            1,
            self._keys(user_id, session_id).context_compaction_lock,
            token,
        )
        return self._result(result)[0] == "1"

    async def acquire_session_activation_lease(
        self, user_id: str, session_id: str, token: str
    ) -> bool:
        if not token:
            raise ValueError("activation lease token must not be blank")
        result = await self.client.set(
            self._keys(user_id, session_id).activation_lock,
            token,
            nx=True,
            px=60_000,
        )
        return bool(result)

    async def release_session_activation_lease(
        self, user_id: str, session_id: str, token: str
    ) -> bool:
        if not token:
            raise ValueError("activation lease token must not be blank")
        result = await self.client.eval(
            RELEASE_LEASE_SCRIPT,
            1,
            self._keys(user_id, session_id).activation_lock,
            token,
        )
        return self._result(result)[0] == "1"

    @staticmethod
    def _events(values: list[Any]) -> tuple[MemoryEvent, ...]:
        return tuple(
            MemoryEvent.model_validate_json(AsyncRedisMemoryStore._text(value))
            for value in values
        )

    @staticmethod
    def _text(value: Any) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    @classmethod
    def _result(cls, result: Any) -> tuple[str, ...]:
        return tuple(cls._text(value) for value in result)

    @staticmethod
    def _keys(user_id: str, session_id: str, event_id: str | None = None) -> "_Keys":
        user = safe_component(user_id, "user_id")
        session = safe_component(session_id, "session_id")
        prefix = f"dream:session:{user}:{session}"
        return _Keys(
            sequence=f"{prefix}:sequence",
            messages=f"{prefix}:messages",
            summary=f"{prefix}:summary",
            event=(
                f"{prefix}:event:{safe_component(event_id, 'event_id')}"
                if event_id is not None
                else ""
            ),
            compression_lock=f"{prefix}:compression-lock",
            session_memory_extraction=f"{prefix}:session-memory-extraction",
            context_compaction_lock=f"{prefix}:context-compaction-lock",
            activation_lock=f"{prefix}:activation-lock",
            pending_reservations=f"{prefix}:pending-reservations",
            ccr_summaries=f"{prefix}:ccr-summaries",
        )


class _Keys:
    def __init__(
        self,
        *,
        sequence: str,
        messages: str,
        summary: str,
        event: str,
        compression_lock: str,
        session_memory_extraction: str,
        context_compaction_lock: str,
        activation_lock: str,
        pending_reservations: str,
        ccr_summaries: str,
    ) -> None:
        self.sequence = sequence
        self.messages = messages
        self.summary = summary
        self.event = event
        self.compression_lock = compression_lock
        self.session_memory_extraction = session_memory_extraction
        self.context_compaction_lock = context_compaction_lock
        self.activation_lock = activation_lock
        self.pending_reservations = pending_reservations
        self.ccr_summaries = ccr_summaries
