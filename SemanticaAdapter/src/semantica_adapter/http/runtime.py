"""Environment-driven runtime for the independently deployed HTTP service."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Mapping

from semantica_adapter.domain.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    api_key: str
    authorized_actors: frozenset[tuple[str, str]]
    provenance_storage_path: Path | None
    host: str = "127.0.0.1"
    port: int = 8001


def load_runtime_config(environ: Mapping[str, str] | None = None) -> RuntimeConfig:
    values = os.environ if environ is None else environ
    api_key = values.get("SEMANTICA_ADAPTER_API_KEY", "")
    if not api_key:
        raise ConfigurationError("SEMANTICA_ADAPTER_API_KEY must be configured")

    raw_actors = values.get("SEMANTICA_ADAPTER_AUTHORIZED_ACTORS", "[]")
    try:
        parsed_actors = json.loads(raw_actors)
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            "SEMANTICA_ADAPTER_AUTHORIZED_ACTORS must be a JSON array of [actor_id, role]"
        ) from error
    if not isinstance(parsed_actors, list) or any(
        not isinstance(item, list)
        or len(item) != 2
        or any(not isinstance(value, str) or not value for value in item)
        for item in parsed_actors
    ):
        raise ConfigurationError(
            "SEMANTICA_ADAPTER_AUTHORIZED_ACTORS must be a JSON array of [actor_id, role]"
        )

    raw_path = values.get("SEMANTICA_ADAPTER_PROVENANCE_PATH")
    try:
        port = int(values.get("SEMANTICA_ADAPTER_PORT", "8001"))
    except ValueError as error:
        raise ConfigurationError("SEMANTICA_ADAPTER_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise ConfigurationError("SEMANTICA_ADAPTER_PORT must be between 1 and 65535")

    return RuntimeConfig(
        api_key=api_key,
        authorized_actors=frozenset((item[0], item[1]) for item in parsed_actors),
        provenance_storage_path=Path(raw_path) if raw_path else None,
        host=values.get("SEMANTICA_ADAPTER_HOST", "127.0.0.1"),
        port=port,
    )


def create_runtime_app():
    from semantica_adapter.api.factory import create_local_semantica_service

    from .app import create_app

    config = load_runtime_config()
    service = create_local_semantica_service(
        authorized_actors=set(config.authorized_actors),
        provenance_storage_path=config.provenance_storage_path,
    )
    return create_app(service, api_key=config.api_key)


def main() -> None:
    import uvicorn

    config = load_runtime_config()
    uvicorn.run(create_runtime_app(), host=config.host, port=config.port)


if __name__ == "__main__":
    main()
