from importlib.metadata import metadata

import short_term_memory
from short_term_memory import (
    CompletionResult,
    PreparedTurn,
    ShortTermMemorySettings,
)


def test_distribution_and_import_package_have_standalone_identity() -> None:
    assert metadata("short-term-memory")["Name"] == "short-term-memory"
    assert short_term_memory.__version__ == "0.1.0"


def test_root_package_exports_only_agent_facing_contract() -> None:
    assert short_term_memory.AgentChatClient.__name__ == "AgentChatClient"
    assert not hasattr(short_term_memory, "build_runtime")
    assert ShortTermMemorySettings.__name__ == "ShortTermMemorySettings"
    assert PreparedTurn.__name__ == "PreparedTurn"
    assert CompletionResult.__name__ == "CompletionResult"
