"""Configuration for the standalone short-term memory SDK."""

from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlparse

from short_term_memory.compression.micro_compact import TimeBasedMicroCompactConfig


@dataclass(frozen=True)
class RedisSessionSettings:
    url: str = "redis://127.0.0.1:6379/0"
    ttl_seconds: int = 43_200
    history_turns: int = 10
    context_window_tokens: int = 128_000
    retain_ratio: float = 0.25
    trigger_ratio: float = 0.65
    max_messages: int = 100
    max_session_seconds: int = 14_400


@dataclass(frozen=True)
class HeadroomServiceSettings:
    url: str = ""
    timeout_seconds: float = 300.0
    compression_model: str = "deepseek-v4-flash"
    ccr_ttl_seconds: int = 43_200
    ccr_refresh_seconds: int = 3_600
    max_compression_segments: int = 8


@dataclass(frozen=True)
class ApiSettings:
    host: str = "127.0.0.1"
    port: int = 8_080
    workers: int = 4
    concurrency_limit: int = 100
    redis_pool_size: int = 200
    max_body_bytes: int = 10 * 1024 * 1024
    request_timeout_seconds: float = 10.0
    write_max_batch_events: int = 100
    auth_token: str = ""


@dataclass(frozen=True)
class JournalSettings:
    retention_days: int = 30


@dataclass(frozen=True)
class CompressionQueueSettings:
    worker_concurrency: int = 8
    capacity: int = 10_000
    shutdown_grace_seconds: float = 30.0


@dataclass(frozen=True)
class DeepSeekPublicSettings:
    api_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"


@dataclass(frozen=True)
class ContinuityCompactionSettings:
    enabled: bool = True
    model: str = "deepseek-v4-flash"
    prepare_timeout_seconds: float = 300.0


@dataclass(frozen=True)
class ShortTermMemorySettings:
    environment: str = "development"
    home: str = "~/.dream"
    optimization_scope_secret: str = "development-only-scope-secret"
    redis_session: RedisSessionSettings = field(default_factory=RedisSessionSettings)
    headroom_service: HeadroomServiceSettings = field(
        default_factory=HeadroomServiceSettings
    )
    api: ApiSettings = field(default_factory=ApiSettings)
    journal: JournalSettings = field(default_factory=JournalSettings)
    compression_queue: CompressionQueueSettings = field(
        default_factory=CompressionQueueSettings
    )
    deepseek_public: DeepSeekPublicSettings = field(
        default_factory=DeepSeekPublicSettings
    )
    continuity_compaction: ContinuityCompactionSettings = field(
        default_factory=ContinuityCompactionSettings
    )
    time_based_microcompact: TimeBasedMicroCompactConfig = field(
        default_factory=TimeBasedMicroCompactConfig
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


def _non_negative_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must not be negative")
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
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _plan_trigger_ratio(value: str, name: str) -> float:
    parsed = _positive_float(value, name)
    if not 0.60 <= parsed <= 0.70:
        raise ValueError(f"{name} must be between 0.60 and 0.70")
    return parsed


def _retain_ratio(value: str, name: str) -> float:
    parsed = _positive_float(value, name)
    if not 0.0 <= parsed <= 1.0:
        raise ValueError(f"{name} must be between 0.0 and 1.0")
    return parsed


def _http_service_url(value: str, name: str) -> str:
    if not value:
        return ""
    normalized = value.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute HTTP URL")
    return normalized


def _required_http_url(value: str, name: str) -> str:
    return _http_service_url(_non_blank(value, name), name)


def _tcp_port(value: str, name: str) -> int:
    parsed = _positive_int(value, name)
    if parsed > 65_535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return parsed


def _non_blank(value: str, name: str) -> str:
    if not value:
        raise ValueError(f"{name} must not be blank")
    return value


def resolve_env_file(
    path: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Resolve the .env path, or None to load process environment only.

    Priority:
      1. Explicit ``path`` argument.
      2. ``SHORT_TERM_MEMORY_ENV_FILE`` environment variable.
      3. ``.env`` in the current working directory (if present).
    """
    candidate = path
    if candidate is None:
        candidate = os.environ.get("SHORT_TERM_MEMORY_ENV_FILE")
    if candidate is None:
        candidate = Path.cwd() / ".env"
    resolved = Path(candidate).expanduser().resolve()
    return resolved if resolved.is_file() else None


def load_settings(path: Path | None = None) -> ShortTermMemorySettings:
    """Load dotenv values with process environment taking priority.

    ``path`` is an explicit .env file; if omitted the path comes from
    :func:`resolve_env_file` (env var ``SHORT_TERM_MEMORY_ENV_FILE`` then
    ``./.env`` in the working directory).
    """

    file_values = (
        _read_env_file(resolved) if (resolved := resolve_env_file(path)) else {}
    )

    def value(name: str, default: str = "") -> str:
        return os.environ.get(name, file_values.get(name, default)).strip()

    environment = value("SHORT_TERM_MEMORY_ENV", "development").casefold()
    if environment not in {"development", "production"}:
        raise ValueError(
            "SHORT_TERM_MEMORY_ENV must be development or production"
        )

    headroom = HeadroomServiceSettings(
        url=_http_service_url(
            value("HEADROOM_SERVICE_URL", ""), "HEADROOM_SERVICE_URL"
        ),
        timeout_seconds=_positive_float(
            value("HEADROOM_SERVICE_TIMEOUT_SECONDS", "300"),
            "HEADROOM_SERVICE_TIMEOUT_SECONDS",
        ),
        compression_model=_non_blank(
            value("HEADROOM_COMPRESSION_MODEL", "deepseek-v4-flash"),
            "HEADROOM_COMPRESSION_MODEL",
        ),
        ccr_ttl_seconds=_positive_int(
            value("HEADROOM_CCR_TTL_SECONDS", "43200"),
            "HEADROOM_CCR_TTL_SECONDS",
        ),
        ccr_refresh_seconds=_positive_int(
            value("HEADROOM_CCR_REFRESH_SECONDS", "3600"),
            "HEADROOM_CCR_REFRESH_SECONDS",
        ),
        max_compression_segments=_positive_int(
            value("HEADROOM_MAX_COMPRESSION_SEGMENTS", "8"),
            "HEADROOM_MAX_COMPRESSION_SEGMENTS",
        ),
    )
    if environment == "production" and not headroom.url:
        raise ValueError("HEADROOM_SERVICE_URL is required in production")

    scope_secret = value(
        "SHORT_TERM_MEMORY_SCOPE_SECRET", "development-only-scope-secret"
    )
    if environment == "production" and not value(
        "SHORT_TERM_MEMORY_SCOPE_SECRET"
    ):
        raise ValueError(
            "production requires SHORT_TERM_MEMORY_SCOPE_SECRET"
        )

    redis_session = RedisSessionSettings(
        url=value("REDIS_URL", "redis://127.0.0.1:6379/0"),
        ttl_seconds=_positive_int(
            value("REDIS_SESSION_TTL_SECONDS", "43200"),
            "REDIS_SESSION_TTL_SECONDS",
        ),
        history_turns=_positive_int(
            value("REDIS_HISTORY_TURNS", "10"), "REDIS_HISTORY_TURNS"
        ),
        retain_ratio=_retain_ratio(
            value("REDIS_RETAIN_RATIO", "0.25"), "REDIS_RETAIN_RATIO"
        ),
        context_window_tokens=_positive_int(
            value("CONTEXT_WINDOW_TOKENS", "128000"),
            "CONTEXT_WINDOW_TOKENS",
        ),
        trigger_ratio=_plan_trigger_ratio(
            value("HEADROOM_TRIGGER_RATIO", "0.65"),
            "HEADROOM_TRIGGER_RATIO",
        ),
        max_messages=_positive_int(
            value("HEADROOM_MAX_MESSAGES", "100"),
            "HEADROOM_MAX_MESSAGES",
        ),
        max_session_seconds=_positive_int(
            value("HEADROOM_MAX_SESSION_SECONDS", "14400"),
            "HEADROOM_MAX_SESSION_SECONDS",
        ),
    )

    api = ApiSettings(
        host=_non_blank(value("MEMORY_API_HOST", "127.0.0.1"), "MEMORY_API_HOST"),
        port=_tcp_port(value("MEMORY_API_PORT", "8080"), "MEMORY_API_PORT"),
        workers=_positive_int(
            value("MEMORY_API_WORKERS", "4"), "MEMORY_API_WORKERS"
        ),
        concurrency_limit=_positive_int(
            value("MEMORY_API_CONCURRENCY_LIMIT", "100"),
            "MEMORY_API_CONCURRENCY_LIMIT",
        ),
        redis_pool_size=_positive_int(
            value("MEMORY_API_REDIS_POOL_SIZE", "200"),
            "MEMORY_API_REDIS_POOL_SIZE",
        ),
        max_body_bytes=_positive_int(
            value("MEMORY_API_MAX_BODY_BYTES", str(10 * 1024 * 1024)),
            "MEMORY_API_MAX_BODY_BYTES",
        ),
        request_timeout_seconds=_positive_float(
            value("MEMORY_API_REQUEST_TIMEOUT_SECONDS", "10"),
            "MEMORY_API_REQUEST_TIMEOUT_SECONDS",
        ),
        write_max_batch_events=_positive_int(
            value("MEMORY_WRITE_MAX_BATCH_EVENTS", "100"),
            "MEMORY_WRITE_MAX_BATCH_EVENTS",
        ),
        auth_token=value("MEMORY_API_AUTH_TOKEN"),
    )
    journal = JournalSettings(
        retention_days=_positive_int(
            value("JOURNAL_RETENTION_DAYS", "30"), "JOURNAL_RETENTION_DAYS"
        )
    )
    compression_queue = CompressionQueueSettings(
        worker_concurrency=_positive_int(
            value("HEADROOM_COMPRESSION_WORKERS", "8"),
            "HEADROOM_COMPRESSION_WORKERS",
        ),
        capacity=_positive_int(
            value("HEADROOM_QUEUE_CAPACITY", "10000"),
            "HEADROOM_QUEUE_CAPACITY",
        ),
        shutdown_grace_seconds=_positive_float(
            value("MEMORY_WORKER_SHUTDOWN_GRACE_SECONDS", "30"),
            "MEMORY_WORKER_SHUTDOWN_GRACE_SECONDS",
        ),
    )
    deepseek_public = DeepSeekPublicSettings(
        api_url=_required_http_url(
            value("DEEPSEEK_API_URL", "https://api.deepseek.com"),
            "DEEPSEEK_API_URL",
        ),
        model=_non_blank(
            value("DEEPSEEK_MODEL", "deepseek-v4-flash"), "DEEPSEEK_MODEL"
        ),
    )
    continuity_compaction = ContinuityCompactionSettings(
        enabled=_boolean(
            value("CONTINUITY_COMPACTION_ENABLED", "true"),
            "CONTINUITY_COMPACTION_ENABLED",
        ),
        model=_non_blank(
            value("CONTINUITY_COMPACTION_MODEL", deepseek_public.model),
            "CONTINUITY_COMPACTION_MODEL",
        ),
        prepare_timeout_seconds=_positive_float(
            value("COMPACTION_PREPARE_TIMEOUT_SECONDS", "300"),
            "COMPACTION_PREPARE_TIMEOUT_SECONDS",
        ),
    )
    time_based_microcompact = TimeBasedMicroCompactConfig(
        enabled=_boolean(
            value("TIME_BASED_MICROCOMPACT_ENABLED", "false"),
            "TIME_BASED_MICROCOMPACT_ENABLED",
        ),
        gap_threshold_minutes=_positive_float(
            value("TIME_BASED_MICROCOMPACT_GAP_MINUTES", "60"),
            "TIME_BASED_MICROCOMPACT_GAP_MINUTES",
        ),
        keep_recent=_non_negative_int(
            value("TIME_BASED_MICROCOMPACT_KEEP_RECENT", "5"),
            "TIME_BASED_MICROCOMPACT_KEEP_RECENT",
        ),
    )

    return ShortTermMemorySettings(
        environment=environment,
        home=value("SHORT_TERM_MEMORY_HOME", "~/.dream"),
        optimization_scope_secret=scope_secret,
        redis_session=redis_session,
        headroom_service=headroom,
        api=api,
        journal=journal,
        compression_queue=compression_queue,
        deepseek_public=deepseek_public,
        continuity_compaction=continuity_compaction,
        time_based_microcompact=time_based_microcompact,
    )
