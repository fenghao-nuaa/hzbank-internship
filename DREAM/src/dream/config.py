"""DREAM runtime configuration loaded from environment variables."""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dream.curators.backend import OpenAICuratorBackend, SemanticCuratorBackend
from dream.extraction.backend import DeterministicReviewBackend, ReviewBackend
from dream.extraction.llm_backend import OpenAIReviewBackend
from dream.memory.writeback import (
    DeterministicWritebackBackend,
    WritebackBackend,
)


@dataclass(frozen=True)
class InternshipSourceSettings:
    enabled: bool = False
    url: str = ""
    api_key: str = ""
    tenant_id: str = ""
    agent_id: str = ""
    batch_size: int = 100
    timeout_seconds: float = 15.0
    interval_seconds: int = 300


@dataclass(frozen=True)
class DreamSettings:
    home: str = "~/.dream"
    review_backend: str = "deterministic"
    review_model: str = ""
    review_base_url: str | None = None
    review_api_key: str = ""
    review_max_completion_tokens: int = 4096
    review_idle_hours: float = 2.0
    review_max_batch_tokens: int = 16_000
    review_max_batch_events: int = 20
    review_max_wait_hours: float = 24.0
    timezone: str = "Asia/Shanghai"
    curator_daily_hour: int = 3
    llm_structured_mode: str = "auto"
    llm_timeout_seconds: float = 90.0
    llm_trust_env: bool = True
    dream_deadline_seconds: float = 300.0
    curator_backend: str = "inherit"
    curator_consolidate: bool = False
    curator_consolidate_interval_hours: float = 168.0
    curator_consolidate_min_idle_hours: float = 2.0
    curator_model: str = ""
    curator_base_url: str | None = None
    curator_api_key: str = ""
    curator_max_completion_tokens: int = 3000
    character_definition_limit: int = 3200
    user_persona_limit: int = 1200
    validation_require_active_writeback: bool = False
    internship_source: InternshipSourceSettings = field(
        default_factory=InternshipSourceSettings
    )


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"invalid .env entry at line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"invalid .env key at line {line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be positive")
    return parsed


def _positive_float(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _boolean(value: str, name: str) -> bool:
    normalized = value.casefold()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _daily_hour(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError("DREAM_CURATOR_DAILY_HOUR must be an integer") from exc
    if not 0 <= parsed <= 23:
        raise ValueError("DREAM_CURATOR_DAILY_HOUR must be between 0 and 23")
    return parsed


def load_settings(path: Path | None = None) -> DreamSettings:
    """Load settings from a dotenv file, with process environment taking priority."""

    file_values = _read_env_file(path) if path is not None else {}

    def value(name: str, default: str = "") -> str:
        return os.environ.get(name, file_values.get(name, default)).strip()

    review_backend = value("DREAM_REVIEW_BACKEND", "deterministic").lower()
    if review_backend not in {"deterministic", "openai"}:
        raise ValueError("DREAM_REVIEW_BACKEND must be deterministic or openai")

    curator_backend = value("DREAM_CURATOR_BACKEND", "inherit").lower()
    if curator_backend not in {"inherit", "deterministic", "openai"}:
        raise ValueError(
            "DREAM_CURATOR_BACKEND must be inherit, deterministic, or openai"
        )
    structured_mode = value("DREAM_LLM_STRUCTURED_MODE", "auto").lower()
    if structured_mode not in {"auto", "tools", "json"}:
        raise ValueError("DREAM_LLM_STRUCTURED_MODE must be auto, tools, or json")
    timezone_name = value("DREAM_TIMEZONE", "Asia/Shanghai")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("DREAM_TIMEZONE must name an installed IANA timezone") from exc

    source = InternshipSourceSettings(
        enabled=_boolean(
            value("DREAM_INTERNSHIP_SOURCE_ENABLED", "false"),
            "DREAM_INTERNSHIP_SOURCE_ENABLED",
        ),
        url=value("DREAM_INTERNSHIP_SOURCE_URL"),
        api_key=value("DREAM_INTERNSHIP_SOURCE_API_KEY"),
        tenant_id=value("DREAM_INTERNSHIP_SOURCE_TENANT_ID"),
        agent_id=value("DREAM_INTERNSHIP_SOURCE_AGENT_ID"),
        batch_size=_positive_int(
            value("DREAM_INTERNSHIP_SOURCE_BATCH_SIZE", "100"),
            "DREAM_INTERNSHIP_SOURCE_BATCH_SIZE",
        ),
        timeout_seconds=_positive_float(
            value("DREAM_INTERNSHIP_SOURCE_TIMEOUT_SECONDS", "15"),
            "DREAM_INTERNSHIP_SOURCE_TIMEOUT_SECONDS",
        ),
        interval_seconds=_positive_int(
            value("DREAM_INTERNSHIP_SOURCE_INTERVAL_SECONDS", "300"),
            "DREAM_INTERNSHIP_SOURCE_INTERVAL_SECONDS",
        ),
    )
    if source.enabled and not all((source.url, source.tenant_id, source.agent_id)):
        raise ValueError(
            "enabled Internship source requires URL, tenant ID, and agent ID"
        )

    return DreamSettings(
        home=value("DREAM_HOME", "~/.dream"),
        review_backend=review_backend,
        review_model=value("DREAM_REVIEW_MODEL"),
        review_base_url=value("DREAM_REVIEW_BASE_URL") or None,
        review_api_key=value("DREAM_LLM_API_KEY"),
        review_max_completion_tokens=_positive_int(
            value("DREAM_REVIEW_MAX_COMPLETION_TOKENS", "4096"),
            "DREAM_REVIEW_MAX_COMPLETION_TOKENS",
        ),
        review_idle_hours=_positive_float(
            value("DREAM_REVIEW_IDLE_HOURS", "2"),
            "DREAM_REVIEW_IDLE_HOURS",
        ),
        review_max_batch_tokens=_positive_int(
            value("DREAM_REVIEW_MAX_BATCH_TOKENS", "16000"),
            "DREAM_REVIEW_MAX_BATCH_TOKENS",
        ),
        review_max_batch_events=_positive_int(
            value("DREAM_REVIEW_MAX_BATCH_EVENTS", "20"),
            "DREAM_REVIEW_MAX_BATCH_EVENTS",
        ),
        review_max_wait_hours=_positive_float(
            value("DREAM_REVIEW_MAX_WAIT_HOURS", "24"),
            "DREAM_REVIEW_MAX_WAIT_HOURS",
        ),
        timezone=timezone_name,
        curator_daily_hour=_daily_hour(value("DREAM_CURATOR_DAILY_HOUR", "3")),
        llm_structured_mode=structured_mode,
        llm_timeout_seconds=_positive_float(
            value("DREAM_LLM_TIMEOUT_SECONDS", "90"),
            "DREAM_LLM_TIMEOUT_SECONDS",
        ),
        llm_trust_env=_boolean(
            value("DREAM_LLM_TRUST_ENV", "true"),
            "DREAM_LLM_TRUST_ENV",
        ),
        dream_deadline_seconds=_positive_float(
            value("DREAM_DEADLINE_SECONDS", "300"),
            "DREAM_DEADLINE_SECONDS",
        ),
        curator_backend=curator_backend,
        curator_consolidate=_boolean(
            value("DREAM_CURATOR_CONSOLIDATE", "false"),
            "DREAM_CURATOR_CONSOLIDATE",
        ),
        curator_consolidate_interval_hours=_positive_float(
            value("DREAM_CURATOR_CONSOLIDATE_INTERVAL_HOURS", "168"),
            "DREAM_CURATOR_CONSOLIDATE_INTERVAL_HOURS",
        ),
        curator_consolidate_min_idle_hours=_positive_float(
            value("DREAM_CURATOR_CONSOLIDATE_MIN_IDLE_HOURS", "2"),
            "DREAM_CURATOR_CONSOLIDATE_MIN_IDLE_HOURS",
        ),
        curator_model=value("DREAM_CURATOR_MODEL"),
        curator_base_url=value("DREAM_CURATOR_BASE_URL") or None,
        curator_api_key=value("DREAM_CURATOR_LLM_API_KEY"),
        curator_max_completion_tokens=_positive_int(
            value("DREAM_CURATOR_MAX_COMPLETION_TOKENS", "3000"),
            "DREAM_CURATOR_MAX_COMPLETION_TOKENS",
        ),
        character_definition_limit=_positive_int(
            value("DREAM_CHARACTER_DEFINITION_LIMIT", "3200"),
            "DREAM_CHARACTER_DEFINITION_LIMIT",
        ),
        user_persona_limit=_positive_int(
            value("DREAM_USER_PERSONA_LIMIT", "1200"),
            "DREAM_USER_PERSONA_LIMIT",
        ),
        validation_require_active_writeback=_boolean(
            value("DREAM_VALIDATION_REQUIRE_ACTIVE_WRITEBACK", "false"),
            "DREAM_VALIDATION_REQUIRE_ACTIVE_WRITEBACK",
        ),
        internship_source=source,
    )


def build_review_backend(
    settings: DreamSettings,
    *,
    client_factory: Callable[..., object] | None = None,
    http_client_factory: Callable[..., object] | None = None,
) -> ReviewBackend:
    if settings.review_backend == "deterministic":
        return DeterministicReviewBackend()
    if not settings.review_model:
        raise ValueError("DREAM_REVIEW_MODEL is required for the openai backend")
    if not settings.review_api_key:
        raise ValueError("missing LLM API key: DREAM_LLM_API_KEY")
    if client_factory is None:
        from openai import OpenAI

        client_factory = OpenAI
    client_kwargs: dict[str, object] = {
        "api_key": settings.review_api_key,
        "max_retries": 0,
        "timeout": settings.llm_timeout_seconds,
    }
    if settings.review_base_url:
        client_kwargs["base_url"] = settings.review_base_url
    if not settings.llm_trust_env:
        if http_client_factory is None:
            from openai import DefaultHttpxClient

            http_client_factory = DefaultHttpxClient
        client_kwargs["http_client"] = http_client_factory(
            timeout=settings.llm_timeout_seconds,
            trust_env=False,
        )
    client = client_factory(**client_kwargs)
    return OpenAIReviewBackend(
        client=client,
        model=settings.review_model,
        max_completion_tokens=settings.review_max_completion_tokens,
        structured_mode=settings.llm_structured_mode,
    )


def build_curator_backend(
    settings: DreamSettings,
    *,
    client_factory: Callable[..., object] | None = None,
    http_client_factory: Callable[..., object] | None = None,
) -> SemanticCuratorBackend | None:
    if not settings.curator_consolidate:
        return None
    backend = settings.curator_backend
    if backend == "deterministic":
        raise ValueError(
            "DREAM_CURATOR_CONSOLIDATE requires an openai or inherited curator backend"
        )
    if backend == "inherit" and settings.review_backend == "deterministic":
        return None
    model = settings.curator_model or settings.review_model
    if not model:
        raise ValueError(
            "DREAM_CURATOR_MODEL or DREAM_REVIEW_MODEL is required for LLM curation"
        )
    api_key = settings.curator_api_key or settings.review_api_key
    if not api_key:
        raise ValueError(
            "missing LLM API key: DREAM_CURATOR_LLM_API_KEY or DREAM_LLM_API_KEY"
        )
    if client_factory is None:
        from openai import OpenAI

        client_factory = OpenAI
    base_url = settings.curator_base_url or settings.review_base_url
    client_kwargs: dict[str, object] = {
        "api_key": api_key,
        "max_retries": 0,
        "timeout": settings.llm_timeout_seconds,
    }
    if base_url:
        client_kwargs["base_url"] = base_url
    if not settings.llm_trust_env:
        if http_client_factory is None:
            from openai import DefaultHttpxClient

            http_client_factory = DefaultHttpxClient
        client_kwargs["http_client"] = http_client_factory(
            timeout=settings.llm_timeout_seconds,
            trust_env=False,
        )
    client = client_factory(**client_kwargs)
    return OpenAICuratorBackend(
        client=client,
        model=model,
        max_completion_tokens=settings.curator_max_completion_tokens,
        structured_mode=settings.llm_structured_mode,
    )


def build_writeback_backend(
    settings: DreamSettings,
    *,
    client_factory: Callable[..., object] | None = None,
) -> WritebackBackend:
    del client_factory
    return DeterministicWritebackBackend()
