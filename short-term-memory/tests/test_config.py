from pathlib import Path

import pytest

from short_term_memory.config import load_settings


ENV_NAMES = (
    "SHORT_TERM_MEMORY_HOME",
    "SHORT_TERM_MEMORY_ENV",
    "SHORT_TERM_MEMORY_SCOPE_SECRET",
    "REDIS_URL",
    "REDIS_SESSION_TTL_SECONDS",
    "REDIS_HISTORY_TURNS",
    "CONTEXT_WINDOW_TOKENS",
    "HEADROOM_SERVICE_URL",
    "HEADROOM_SERVICE_TIMEOUT_SECONDS",
    "HEADROOM_COMPRESSION_MODEL",
    "HEADROOM_CCR_TTL_SECONDS",
    "HEADROOM_TRIGGER_RATIO",
    "HEADROOM_MAX_MESSAGES",
    "HEADROOM_MAX_SESSION_SECONDS",
    "MEMORY_API_HOST",
    "MEMORY_API_PORT",
    "MEMORY_API_WORKERS",
    "MEMORY_API_CONCURRENCY_LIMIT",
    "MEMORY_API_REDIS_POOL_SIZE",
    "MEMORY_API_MAX_BODY_BYTES",
    "MEMORY_API_REQUEST_TIMEOUT_SECONDS",
    "MEMORY_WRITE_MAX_BATCH_EVENTS",
    "MEMORY_API_AUTH_TOKEN",
    "JOURNAL_RETENTION_DAYS",
    "HEADROOM_CCR_REFRESH_SECONDS",
    "HEADROOM_MAX_COMPRESSION_SEGMENTS",
    "HEADROOM_COMPRESSION_WORKERS",
    "MEMORY_WORKER_SHUTDOWN_GRACE_SECONDS",
    "HEADROOM_QUEUE_CAPACITY",
    "DEEPSEEK_API_URL",
    "DEEPSEEK_MODEL",
    "CONTINUITY_COMPACTION_ENABLED",
    "CONTINUITY_COMPACTION_MODEL",
    "COMPACTION_PREPARE_TIMEOUT_SECONDS",
    "TIME_BASED_MICROCOMPACT_ENABLED",
    "TIME_BASED_MICROCOMPACT_GAP_MINUTES",
    "TIME_BASED_MICROCOMPACT_KEEP_RECENT",
)


@pytest.fixture(autouse=True)
def clean_short_term_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_default_short_term_settings(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.env")

    assert settings.environment == "development"
    assert settings.home == "~/.dream"
    assert settings.redis_session.url == "redis://127.0.0.1:6379/0"
    assert settings.redis_session.ttl_seconds == 43_200
    assert settings.redis_session.history_turns == 10
    assert settings.redis_session.trigger_ratio == 0.65
    assert settings.headroom_service.ccr_ttl_seconds == 43_200


def test_http_memory_defaults_are_teacher_visible() -> None:
    settings = load_settings()

    assert settings.api.concurrency_limit == 100
    assert settings.api.redis_pool_size == 200
    assert settings.api.max_body_bytes == 10 * 1024 * 1024
    assert settings.journal.retention_days == 30
    assert settings.compression_queue.worker_concurrency == 8
    assert settings.compression_queue.shutdown_grace_seconds == 30.0
    assert settings.headroom_service.ccr_ttl_seconds == 43_200
    assert settings.headroom_service.compression_model == "deepseek-v4-flash"
    assert settings.deepseek_public.model == "deepseek-v4-flash"
    assert settings.deepseek_public.api_url == "https://api.deepseek.com"
    assert settings.continuity_compaction.enabled is True
    assert settings.continuity_compaction.model == settings.deepseek_public.model
    assert settings.continuity_compaction.prepare_timeout_seconds == 300.0
    assert settings.time_based_microcompact.enabled is False
    assert settings.time_based_microcompact.gap_threshold_minutes == 60.0
    assert settings.time_based_microcompact.keep_recent == 5


def test_time_based_microcompact_settings_allow_zero_keep_with_runtime_floor(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "TIME_BASED_MICROCOMPACT_ENABLED=true\n"
        "TIME_BASED_MICROCOMPACT_GAP_MINUTES=45\n"
        "TIME_BASED_MICROCOMPACT_KEEP_RECENT=0\n",
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings.time_based_microcompact.enabled is True
    assert settings.time_based_microcompact.gap_threshold_minutes == 45
    assert settings.time_based_microcompact.keep_recent == 0


def test_continuity_compaction_settings_are_independent_from_read_timeout(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "MEMORY_API_REQUEST_TIMEOUT_SECONDS=10\n"
        "COMPACTION_PREPARE_TIMEOUT_SECONDS=275\n"
        "CONTINUITY_COMPACTION_ENABLED=false\n"
        "CONTINUITY_COMPACTION_MODEL=compact-model\n",
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings.api.request_timeout_seconds == 10
    assert settings.continuity_compaction.prepare_timeout_seconds == 275
    assert settings.continuity_compaction.enabled is False
    assert settings.continuity_compaction.model == "compact-model"


def test_new_memory_settings_parse_validated_environment(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "MEMORY_API_PORT=9000\n"
        "MEMORY_WRITE_MAX_BATCH_EVENTS=5\n"
        "JOURNAL_RETENTION_DAYS=60\n"
        "HEADROOM_CCR_REFRESH_SECONDS=600\n"
        "MEMORY_WORKER_SHUTDOWN_GRACE_SECONDS=7.5\n",
        encoding="utf-8",
    )

    settings = load_settings(path)

    assert settings.api.port == 9000
    assert settings.api.write_max_batch_events == 5
    assert settings.journal.retention_days == 60
    assert settings.headroom_service.ccr_refresh_seconds == 600
    assert settings.compression_queue.shutdown_grace_seconds == 7.5


def test_deepseek_api_url_must_be_a_non_blank_absolute_http_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_URL", "")

    with pytest.raises(ValueError, match="DEEPSEEK_API_URL"):
        load_settings()


def test_memory_api_port_must_be_in_the_tcp_port_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_API_PORT", "70000")

    with pytest.raises(ValueError, match="MEMORY_API_PORT"):
        load_settings()


def test_process_environment_overrides_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    path.write_text("REDIS_HISTORY_TURNS=5\n", encoding="utf-8")
    monkeypatch.setenv("REDIS_HISTORY_TURNS", "12")

    assert load_settings(path).redis_session.history_turns == 12


def test_production_requires_headroom_url(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "SHORT_TERM_MEMORY_ENV=production\n"
        "SHORT_TERM_MEMORY_SCOPE_SECRET=secret\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="HEADROOM_SERVICE_URL"):
        load_settings(path)


def test_production_requires_scope_secret(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "SHORT_TERM_MEMORY_ENV=production\n"
        "HEADROOM_SERVICE_URL=http://127.0.0.1:8787\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="SHORT_TERM_MEMORY_SCOPE_SECRET"):
        load_settings(path)


def test_production_accepts_complete_external_service_config(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "SHORT_TERM_MEMORY_ENV=production\n"
        "SHORT_TERM_MEMORY_SCOPE_SECRET=secret\n"
        "HEADROOM_SERVICE_URL=http://headroom:8787/\n",
        encoding="utf-8",
    )

    settings = load_settings(path)

    assert settings.environment == "production"
    assert settings.headroom_service.url == "http://headroom:8787"


@pytest.mark.parametrize("ratio", ["0.59", "0.71"])
def test_trigger_ratio_must_match_plan(tmp_path: Path, ratio: str) -> None:
    path = tmp_path / ".env"
    path.write_text(f"HEADROOM_TRIGGER_RATIO={ratio}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="between 0.60 and 0.70"):
        load_settings(path)
