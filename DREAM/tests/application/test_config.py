from pathlib import Path

import pytest

from dream.config import (
    DreamSettings,
    build_curator_backend,
    build_review_backend,
    build_writeback_backend,
    load_settings,
)
from dream.api import create_app
from dream.curators.backend import OpenAICuratorBackend
from dream.extraction.backend import DeterministicReviewBackend
from dream.extraction.llm_backend import OpenAIReviewBackend
from dream.memory.writeback import DeterministicWritebackBackend


def test_missing_env_file_uses_safe_deterministic_backend(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / ".env")
    assert isinstance(build_review_backend(settings), DeterministicReviewBackend)


def test_internship_source_defaults_to_disabled(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / ".env")
    assert settings.internship_source.enabled is False
    assert settings.validation_require_active_writeback is False
    assert settings.review_idle_hours == 2
    assert settings.review_max_completion_tokens == 4096
    assert settings.review_max_batch_tokens == 16_000
    assert settings.review_max_batch_events == 20
    assert settings.review_max_wait_hours == 24
    assert settings.timezone == "Asia/Shanghai"
    assert settings.curator_daily_hour == 3
    assert settings.curator_consolidate is False
    assert settings.curator_consolidate_interval_hours == 168
    assert settings.curator_consolidate_min_idle_hours == 2
    assert settings.dream_deadline_seconds == 300
    assert settings.llm_timeout_seconds == 90
    assert settings.llm_trust_env is True


def test_validation_barrier_can_be_enabled(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_VALIDATION_REQUIRE_ACTIVE_WRITEBACK=true\n",
        encoding="utf-8",
    )

    assert load_settings(env_file).validation_require_active_writeback is True


def test_adaptive_review_schedule_loads_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_REVIEW_IDLE_HOURS=3.5\n"
        "DREAM_REVIEW_MAX_BATCH_TOKENS=12000\n"
        "DREAM_REVIEW_MAX_BATCH_EVENTS=15\n"
        "DREAM_REVIEW_MAX_WAIT_HOURS=18\n",
        encoding="utf-8",
    )

    settings = load_settings(env_file)

    assert settings.review_idle_hours == 3.5
    assert settings.review_max_batch_tokens == 12_000
    assert settings.review_max_batch_events == 15
    assert settings.review_max_wait_hours == 18


def test_adaptive_review_schedule_rejects_non_positive_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DREAM_REVIEW_IDLE_HOURS=0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="DREAM_REVIEW_IDLE_HOURS must be positive"):
        load_settings(env_file)


def test_internship_source_loads_pull_settings(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_INTERNSHIP_SOURCE_ENABLED=true\n"
        "DREAM_INTERNSHIP_SOURCE_URL=http://127.0.0.1:8000/v1/memory/dream-export\n"
        "DREAM_INTERNSHIP_SOURCE_API_KEY=source-secret\n"
        "DREAM_INTERNSHIP_SOURCE_TENANT_ID=acme\n"
        "DREAM_INTERNSHIP_SOURCE_AGENT_ID=assistant\n"
        "DREAM_INTERNSHIP_SOURCE_BATCH_SIZE=25\n"
        "DREAM_INTERNSHIP_SOURCE_TIMEOUT_SECONDS=7.5\n"
        "DREAM_INTERNSHIP_SOURCE_INTERVAL_SECONDS=120\n",
        encoding="utf-8",
    )
    source = load_settings(env_file).internship_source
    assert source.enabled is True
    assert source.url.endswith("/v1/memory/dream-export")
    assert source.api_key == "source-secret"
    assert source.tenant_id == "acme"
    assert source.agent_id == "assistant"
    assert source.batch_size == 25
    assert source.timeout_seconds == 7.5
    assert source.interval_seconds == 120


def test_openai_compatible_backend_uses_env_file_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_REVIEW_BACKEND=openai\n"
        "DREAM_REVIEW_MODEL=review-model\n"
        "DREAM_REVIEW_BASE_URL=https://llm.example/v1\n"
        "DREAM_LLM_API_KEY=secret-key\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def client_factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    backend = build_review_backend(
        load_settings(env_file), client_factory=client_factory
    )

    assert isinstance(backend, OpenAIReviewBackend)
    assert backend.model == "review-model"
    assert captured == {
        "api_key": "secret-key",
        "base_url": "https://llm.example/v1",
        "max_retries": 0,
        "timeout": 90,
    }


def test_openai_review_backend_can_ignore_environment_proxy(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_REVIEW_BACKEND=openai\n"
        "DREAM_REVIEW_MODEL=review-model\n"
        "DREAM_LLM_API_KEY=secret-key\n"
        "DREAM_LLM_TRUST_ENV=false\n",
        encoding="utf-8",
    )
    client_kwargs: dict[str, object] = {}
    http_client_kwargs: dict[str, object] = {}
    http_client = object()

    def client_factory(**kwargs: object) -> object:
        client_kwargs.update(kwargs)
        return object()

    def http_client_factory(**kwargs: object) -> object:
        http_client_kwargs.update(kwargs)
        return http_client

    settings = load_settings(env_file)
    build_review_backend(
        settings,
        client_factory=client_factory,
        http_client_factory=http_client_factory,
    )

    assert settings.llm_trust_env is False
    assert http_client_kwargs == {"timeout": 90, "trust_env": False}
    assert client_kwargs["http_client"] is http_client


def test_openai_curator_backend_uses_same_proxy_policy(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_REVIEW_BACKEND=openai\n"
        "DREAM_REVIEW_MODEL=review-model\n"
        "DREAM_LLM_API_KEY=secret-key\n"
        "DREAM_CURATOR_CONSOLIDATE=true\n"
        "DREAM_LLM_TRUST_ENV=false\n",
        encoding="utf-8",
    )
    client_kwargs: dict[str, object] = {}
    http_client_kwargs: dict[str, object] = {}
    http_client = object()

    def client_factory(**kwargs: object) -> object:
        client_kwargs.update(kwargs)
        return object()

    def http_client_factory(**kwargs: object) -> object:
        http_client_kwargs.update(kwargs)
        return http_client

    build_curator_backend(
        load_settings(env_file),
        client_factory=client_factory,
        http_client_factory=http_client_factory,
    )

    assert http_client_kwargs == {"timeout": 90, "trust_env": False}
    assert client_kwargs["http_client"] is http_client


def test_process_environment_overrides_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DREAM_REVIEW_MODEL=file-model\n", encoding="utf-8")
    monkeypatch.setenv("DREAM_REVIEW_MODEL", "process-model")

    assert load_settings(env_file).review_model == "process-model"


def test_openai_backend_rejects_missing_api_key() -> None:
    settings = DreamSettings(
        review_backend="openai",
        review_model="review-model",
        review_base_url=None,
        review_api_key="",
        review_max_completion_tokens=2000,
    )
    with pytest.raises(ValueError, match="DREAM_LLM_API_KEY"):
        build_review_backend(settings, client_factory=lambda **_: object())


def test_api_loads_review_backend_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_REVIEW_BACKEND=openai\n"
        "DREAM_REVIEW_MODEL=review-model\n"
        "DREAM_LLM_API_KEY=secret-key\n",
        encoding="utf-8",
    )
    app = create_app(
        tmp_path,
        env_file=env_file,
        client_factory=lambda **_: object(),
    )
    assert isinstance(app.state.dream_service.reviewer.backend, OpenAIReviewBackend)
    assert app.state.dream_service.semantic_curator_backend is None


def test_curator_backend_can_inherit_the_review_model(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_REVIEW_BACKEND=openai\n"
        "DREAM_REVIEW_MODEL=shared-model\n"
        "DREAM_LLM_API_KEY=secret-key\n"
        "DREAM_CURATOR_BACKEND=inherit\n",
        encoding="utf-8",
    )
    settings = load_settings(env_file)
    assert build_curator_backend(settings, client_factory=lambda **_: object()) is None

    env_file.write_text(
        env_file.read_text(encoding="utf-8") + "DREAM_CURATOR_CONSOLIDATE=true\n",
        encoding="utf-8",
    )
    backend = build_curator_backend(
        load_settings(env_file), client_factory=lambda **_: object()
    )
    assert isinstance(backend, OpenAICuratorBackend)
    assert backend.model == "shared-model"


def test_semantic_curator_schedule_loads_only_when_enabled(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_REVIEW_BACKEND=openai\n"
        "DREAM_REVIEW_MODEL=shared-model\n"
        "DREAM_LLM_API_KEY=secret-key\n"
        "DREAM_CURATOR_CONSOLIDATE=true\n"
        "DREAM_CURATOR_CONSOLIDATE_INTERVAL_HOURS=120\n"
        "DREAM_CURATOR_CONSOLIDATE_MIN_IDLE_HOURS=3.5\n",
        encoding="utf-8",
    )

    settings = load_settings(env_file)
    backend = build_curator_backend(settings, client_factory=lambda **_: object())

    assert settings.curator_consolidate is True
    assert settings.curator_consolidate_interval_hours == 120
    assert settings.curator_consolidate_min_idle_hours == 3.5
    assert isinstance(backend, OpenAICuratorBackend)


def test_structured_llm_mode_is_loaded_and_passed_to_backends(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_REVIEW_BACKEND=openai\n"
        "DREAM_REVIEW_MODEL=agnes-model\n"
        "DREAM_LLM_API_KEY=secret-key\n"
        "DREAM_LLM_STRUCTURED_MODE=json\n"
        "DREAM_CURATOR_CONSOLIDATE=true\n",
        encoding="utf-8",
    )
    settings = load_settings(env_file)

    review = build_review_backend(settings, client_factory=lambda **_: object())
    curator = build_curator_backend(settings, client_factory=lambda **_: object())

    assert settings.llm_structured_mode == "json"
    assert review.structured_mode == "json"
    assert curator is not None
    assert curator.structured_mode == "json"


def test_writeback_limits_and_local_backend_are_configured(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_REVIEW_BACKEND=openai\n"
        "DREAM_REVIEW_MODEL=agnes-model\n"
        "DREAM_LLM_API_KEY=secret-key\n"
        "DREAM_CHARACTER_DEFINITION_LIMIT=2500\n"
        "DREAM_USER_PERSONA_LIMIT=900\n",
        encoding="utf-8",
    )
    settings = load_settings(env_file)
    backend = build_writeback_backend(settings, client_factory=lambda **_: object())

    assert settings.character_definition_limit == 2500
    assert settings.user_persona_limit == 900
    assert isinstance(backend, DeterministicWritebackBackend)


def test_deadline_and_model_timeout_load_from_env(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_DEADLINE_SECONDS=240\nDREAM_LLM_TIMEOUT_SECONDS=45\n",
        encoding="utf-8",
    )

    settings = load_settings(env_file)

    assert settings.dream_deadline_seconds == 240
    assert settings.llm_timeout_seconds == 45
