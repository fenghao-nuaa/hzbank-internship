class FakePipeline:
    def __init__(self, client: "FakeRedis") -> None:
        self.client = client
        self.commands: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _queue(self, name: str, *args: object, **kwargs: object) -> "FakePipeline":
        self.commands.append((name, args, kwargs))
        return self

    def rpush(self, key: str, value: str) -> "FakePipeline":
        return self._queue("rpush", key, value)

    def ltrim(self, key: str, start: int, end: int) -> "FakePipeline":
        return self._queue("ltrim", key, start, end)

    def expire(self, key: str, seconds: int) -> "FakePipeline":
        return self._queue("expire", key, seconds)

    def set(self, key: str, value: str, *, ex: int) -> "FakePipeline":
        return self._queue("set", key, value, ex=ex)

    def execute(self) -> list[object]:
        return [
            getattr(self.client, name)(*args, **kwargs)
            for name, args, kwargs in self.commands
        ]


class FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.expirations: list[tuple[str, int]] = []
        self.pipeline_transactions: list[bool] = []

    def pipeline(self, *, transaction: bool = True) -> FakePipeline:
        self.pipeline_transactions.append(transaction)
        return FakePipeline(self)

    def rpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        values = self.lists.get(key, [])
        start = max(0, len(values) + start) if start < 0 else start
        end = len(values) - 1 if end == -1 else end
        return values[start : end + 1]

    def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))

    def ltrim(self, key: str, start: int, end: int) -> bool:
        self.lists[key] = self.lrange(key, start, end)
        return True

    def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def exists(self, *keys: str) -> int:
        return sum(key in self.lists or key in self.values for key in keys)

    def expire(self, key: str, seconds: int) -> bool:
        self.expirations.append((key, seconds))
        if key not in self.lists and key not in self.values:
            return False
        self.ttls[key] = seconds
        return True

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            deleted += int(key in self.lists or key in self.values)
            self.lists.pop(key, None)
            self.values.pop(key, None)
            self.ttls.pop(key, None)
        return deleted


class AsyncFakeRedis:
    """Async Redis double with manual expiration; it does not advance real time."""

    def __init__(self) -> None:
        import asyncio

        self.lists: dict[str, list[str]] = {}
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.ttls: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def eval(self, script: str, numkeys: int, *args: str) -> list[str]:
        keys = args[:numkeys]
        values = args[numkeys:]
        async with self._lock:
            if "dream:reserve-event" in script:
                assert numkeys == 3 and len(args) == 6
                sequence_key, event_key, pending_key = keys
                digest, event_ttl, event_id = values
                assert event_key.endswith(f":event:{event_id}")
                record = self.hashes.get(event_key)
                if record is not None:
                    if record["digest"] != digest:
                        return ["conflict", "0"]
                    return [record["status"], record["sequence"]]
                sequence = int(self.values.get(sequence_key, "0")) + 1
                self.values[sequence_key] = str(sequence)
                self.hashes[event_key] = {
                    "digest": digest,
                    "status": "pending",
                    "sequence": str(sequence),
                }
                self.sets.setdefault(pending_key, set()).add(event_id)
                self.ttls[event_key] = int(event_ttl)
                self.ttls[pending_key] = int(event_ttl)
                self.ttls[sequence_key] = int(event_ttl)
                return ["reserved", str(sequence)]
            if "dream:commit-event" in script:
                assert numkeys == 5 and len(args) == 10
                sequence_key, messages_key, summary_key, event_key, pending_key = keys
                event_json, sequence, digest, ttl, event_id = values
                assert event_key.endswith(f":event:{event_id}")
                record = self.hashes.get(event_key)
                if record is None:
                    return ["missing"]
                if record["sequence"] != sequence:
                    return ["sequence_conflict"]
                if record["digest"] != digest:
                    return ["digest_conflict"]
                if record["status"] == "committed":
                    return ["duplicate"]
                self.lists.setdefault(messages_key, []).append(event_json)
                record["status"] = "committed"
                self.sets.setdefault(pending_key, set()).discard(event_id)
                if not self.sets[pending_key]:
                    self.sets.pop(pending_key, None)
                    self.ttls.pop(pending_key, None)
                for key in (messages_key, summary_key, event_key, sequence_key):
                    if key in self.lists or key in self.values or key in self.hashes:
                        self.ttls[key] = int(ttl)
                return ["committed"]
            if "dream:restore-originals-v1" in script:
                import json

                assert numkeys == 3 and len(args) == 7
                sequence_key, messages_key, pending_key = keys
                originals = json.loads(values[0])
                event_prefix, ttl, maximum = values[1:]
                for event in originals:
                    record = self.hashes.get(f"{event_prefix}{event['event_id']}")
                    if record is not None and (
                        record["digest"] != event["sha256"]
                        or record["sequence"] != str(event["sequence"])
                    ):
                        return ["conflict"]
                if self.lists.get(messages_key) or sequence_key in self.values or self.sets.get(pending_key):
                    return ["not_restored"]
                for event in originals:
                    event_key = f"{event_prefix}{event['event_id']}"
                    self.hashes[event_key] = {
                        "digest": event["sha256"],
                        "status": "committed",
                        "sequence": str(event["sequence"]),
                    }
                    self.ttls[event_key] = int(ttl)
                    self.lists.setdefault(messages_key, []).append(json.dumps(event, separators=(",", ":")))
                self.values[sequence_key] = str(maximum)
                self.ttls[sequence_key] = int(ttl)
                self.ttls[messages_key] = int(ttl)
                return ["restored"]
            if "dream:restore-session-projection-v1" in script:
                import json

                assert numkeys == 4 and len(args) == 9
                sequence_key, messages_key, summary_key, pending_key = keys
                originals = json.loads(values[0])
                event_prefix, ttl, latest_sequence, serialized_envelope = values[1:]
                if (
                    sequence_key in self.values
                    or self.lists.get(messages_key)
                    or summary_key in self.values
                    or self.sets.get(pending_key)
                ):
                    return ["not_restored"]
                for event in originals:
                    event_key = f"{event_prefix}{event['event_id']}"
                    self.hashes[event_key] = {
                        "digest": event["sha256"],
                        "status": "committed",
                        "sequence": str(event["sequence"]),
                    }
                    self.ttls[event_key] = int(ttl)
                    self.lists.setdefault(messages_key, []).append(
                        json.dumps(event, separators=(",", ":"))
                    )
                self.values[sequence_key] = latest_sequence
                self.ttls[sequence_key] = int(ttl)
                if originals:
                    self.ttls[messages_key] = int(ttl)
                if serialized_envelope:
                    self.values[summary_key] = serialized_envelope
                    self.ttls[summary_key] = int(ttl)
                return ["restored"]
            if "dream:compare-and-set-envelope" in script:
                assert numkeys == 1 and len(args) == 4
                summary_key = keys[0]
                expected_version, serialized, ttl = values
                current = self.values.get(summary_key)
                if current is not None:
                    import json

                    if str(json.loads(current)["version"]) != expected_version:
                        return ["0"]
                elif expected_version != "0":
                    return ["0"]
                self.values[summary_key] = serialized
                self.ttls[summary_key] = int(ttl)
                return ["1"]
            if "dream:release-compression-lease" in script:
                assert numkeys == 1 and len(args) == 2
                key = keys[0]
                if self.values.get(key) != values[0]:
                    return ["0"]
                self.values.pop(key, None)
                self.ttls.pop(key, None)
                return ["1"]
            if "dream:release-session-memory-extraction" in script:
                import json

                assert numkeys == 1 and len(args) == 2
                key = keys[0]
                current = self.values.get(key)
                if current is None or json.loads(current)["token"] != values[0]:
                    return ["0"]
                self.values.pop(key, None)
                self.ttls.pop(key, None)
                return ["1"]
        raise AssertionError("unsupported Lua script")

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        values = self.lists.get(key, [])
        start = max(0, len(values) + start) if start < 0 else start
        end = len(values) - 1 if end == -1 else end
        return values[start : end + 1]

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def hset(self, key: str, field: str, value: str) -> int:
        async with self._lock:
            self.hashes.setdefault(key, {})[field] = value
            return 1

    async def hgetall(self, key: str) -> dict[str, str]:
        async with self._lock:
            return dict(self.hashes.get(key, {}))

    async def expire(self, key: str, seconds: int) -> bool:
        async with self._lock:
            exists = (
                key in self.lists
                or key in self.values
                or key in self.hashes
                or key in self.sets
            )
            if exists:
                self.ttls[key] = int(seconds)
            return exists

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        px: int | None = None,
    ) -> bool | None:
        async with self._lock:
            if nx and (
                key in self.values
                or key in self.lists
                or key in self.hashes
                or key in self.sets
            ):
                return None
            self.values[key] = value
            if px is not None:
                self.ttls[key] = px // 1000
            return True

    def expire_now(self, key: str) -> bool:
        """Explicitly expire one key for a test without advancing a fake clock."""

        exists = (
            key in self.lists
            or key in self.values
            or key in self.hashes
            or key in self.sets
        )
        self.lists.pop(key, None)
        self.values.pop(key, None)
        self.hashes.pop(key, None)
        self.sets.pop(key, None)
        self.ttls.pop(key, None)
        return exists
