from dataclasses import replace

import pytest
import semantica

from semantica_adapter.adapters.semantica.config import SemanticaConfig
from semantica_adapter.domain.errors import ConfigurationError


def test_config_accepts_exact_semantica_version() -> None:
    config = SemanticaConfig()
    assert config.verify_version() == "0.6.6"
    assert config.advanced_analytics is False


def test_config_rejects_unexpected_semantica_version(monkeypatch) -> None:
    monkeypatch.setattr(semantica, "__version__", "9.9.9")
    with pytest.raises(ConfigurationError, match="0.6.6 required"):
        SemanticaConfig().verify_version()
    assert replace(SemanticaConfig(), strict_version=False).verify_version() == "9.9.9"
