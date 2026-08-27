from pathlib import Path

import yaml


def test_memory_compose_has_four_explicit_independent_services() -> None:
    path = Path("compose.memory.yml")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert set(document["services"]) == {
        "redis",
        "headroom",
        "memory-api",
        "compression-worker",
    }
    assert document["services"]["memory-api"]["command"] == [
        "short-term-memory-api"
    ]
    assert document["services"]["compression-worker"]["command"] == [
        "short-term-memory-worker"
    ]


def test_headroom_proxy_uses_deepseek_upstream_one_worker_and_200_limit() -> None:
    document = yaml.safe_load(Path("compose.memory.yml").read_text(encoding="utf-8"))
    command = document["services"]["headroom"]["command"]

    assert command[0:2] == ["headroom", "proxy"]
    assert command[command.index("--workers") + 1] == "1"
    assert command[command.index("--limit-concurrency") + 1] == "200"
    assert command[command.index("--openai-api-url") + 1] == "https://api.deepseek.com"


def test_compose_contains_only_secret_placeholders_not_secret_values() -> None:
    rendered = Path("compose.memory.yml").read_text(encoding="utf-8")

    assert "DEEPSEEK_API_KEY:" not in rendered
    for variable in (
        "MEMORY_API_AUTH_TOKEN",
        "SHORT_TERM_MEMORY_SCOPE_SECRET",
    ):
        assert f"${{{variable}:?" in rendered
    assert "development-only-scope-secret" not in rendered
    assert "password" not in rendered.casefold()


def test_redis_compose_uses_the_same_pinned_redis_image() -> None:
    standalone = yaml.safe_load(Path("compose.redis.yml").read_text(encoding="utf-8"))
    memory = yaml.safe_load(Path("compose.memory.yml").read_text(encoding="utf-8"))

    assert standalone["services"]["redis"]["image"] == "redis:7.2.15-bookworm"
    assert memory["services"]["redis"]["image"] == "redis:7.2.15-bookworm"


def test_redis_services_enable_aof_everysec_on_named_volumes() -> None:
    standalone = yaml.safe_load(Path("compose.redis.yml").read_text(encoding="utf-8"))
    memory = yaml.safe_load(Path("compose.memory.yml").read_text(encoding="utf-8"))

    for document in (standalone, memory):
        redis = document["services"]["redis"]
        assert redis["command"] == [
            "redis-server",
            "--appendonly",
            "yes",
            "--appendfsync",
            "everysec",
        ]
        assert any(volume.endswith(":/data") for volume in redis["volumes"])
        assert document["volumes"]
