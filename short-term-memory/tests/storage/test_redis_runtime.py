from short_term_memory.storage.redis_runtime import RedisRuntime


class FakePool:
    def __init__(self) -> None:
        self.disconnected = False

    def disconnect(self) -> None:
        self.disconnected = True


class FakeClient:
    def __init__(self) -> None:
        self.ping_calls = 0
        self.closed = False

    def ping(self) -> bool:
        self.ping_calls += 1
        return True

    def close(self) -> None:
        self.closed = True


def test_runtime_reuses_one_pool_and_validates_connection(monkeypatch) -> None:
    pool = FakePool()
    client = FakeClient()
    captured: dict[str, object] = {}

    def from_url(url: str, **kwargs: object) -> FakePool:
        captured.update(url=url, **kwargs)
        return pool

    monkeypatch.setattr(
        "short_term_memory.storage.redis_runtime.redis.ConnectionPool.from_url",
        from_url,
    )
    monkeypatch.setattr(
        "short_term_memory.storage.redis_runtime.redis.Redis",
        lambda *, connection_pool: client,
    )

    runtime = RedisRuntime.connect("redis://redis.internal:6379/2")

    assert runtime.client is client
    assert client.ping_calls == 1
    assert captured == {
        "url": "redis://redis.internal:6379/2",
        "decode_responses": True,
    }

    runtime.close()
    assert client.closed is True
    assert pool.disconnected is True
