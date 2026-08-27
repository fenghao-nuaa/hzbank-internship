"""Semantica backend configuration and compatibility guard."""

from dataclasses import dataclass
from pathlib import Path

from semantica_adapter import SEMANTICA_COMPAT_VERSION
from semantica_adapter.domain.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class SemanticaConfig:
    provenance_storage_path: Path | None = None
    advanced_analytics: bool = False
    strict_version: bool = True

    def verify_version(self) -> str:
        import semantica

        installed = semantica.__version__
        if self.strict_version and installed != SEMANTICA_COMPAT_VERSION:
            raise ConfigurationError(
                f"Semantica {SEMANTICA_COMPAT_VERSION} required, found {installed}"
            )
        return installed
