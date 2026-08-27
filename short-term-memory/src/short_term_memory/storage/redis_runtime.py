"""Lifecycle wrapper for the official redis-py client."""

from dataclasses import dataclass
from typing import Any

import redis


@dataclass
class RedisRuntime:
    pool: Any
    client: Any

    @classmethod
    def connect(cls, url: str) -> "RedisRuntime":
        if not url:
            raise ValueError("Redis URL must not be empty")
        pool = redis.ConnectionPool.from_url(url, decode_responses=True)
        client = redis.Redis(connection_pool=pool)
        client.ping()
        return cls(pool=pool, client=client)

    def close(self) -> None:
        self.client.close()
        self.pool.disconnect()
