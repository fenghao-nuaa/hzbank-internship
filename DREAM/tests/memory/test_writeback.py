from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from dream.memory.artifacts import AtomicArtifactStore
from dream.core.scope import ScopeIds, resolve_scope
from dream.memory.writeback import (
    DeterministicWritebackBackend,
    OpenAIWritebackBackend,
    WritebackService,
    WritebackValidationError,
)


IDS = ScopeIds("dream-lab", "enterprise-colleague", "project-manager")


class RecordingBackend:
    def __init__(self) -> None:
        self.character_input = ""
        self.persona_input = ""

    def render_character(self, decision_rules: str, limit: int) -> str:
        self.character_input = decision_rules
        return "先给结论，并明确风险和下一步。"

    def render_user_persona(self, user_profile: str, limit: int) -> str:
        self.persona_input = user_profile
        return "用户偏好先看结论、负责人和风险。"


def prepared_service(tmp_path: Path, backend: object) -> WritebackService:
    paths = resolve_scope(tmp_path, IDS)
    store = AtomicArtifactStore(paths.agent_root)
    store.write_text(
        Path("DECISION_RULES.md"),
        "# AI Decision Rules\n\n- 先给结论。\n  - Evidence cards: card-1\n",
    )
    store.write_text(
        Path("users/project-manager/USER.md"),
        "用户偏好结论优先。\n<!-- dream-source: evt-1 -->\n",
    )
    return WritebackService(paths, backend=backend)


def test_writeback_backend_receives_ai_and_user_sources_separately(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend()
    service = prepared_service(tmp_path, backend)

    artifacts = service.generate()

    assert backend.character_input.startswith("# AI Decision Rules")
    assert "用户偏好" not in backend.character_input
    assert backend.persona_input.startswith("用户偏好")
    assert "dream-source" not in backend.persona_input
    assert artifacts.character.sha256
    assert artifacts.user_persona.sha256


def test_oversized_writeback_does_not_replace_stable_files(tmp_path: Path) -> None:
    class OversizedBackend(RecordingBackend):
        def render_user_persona(self, user_profile: str, limit: int) -> str:
            return "x" * (limit + 1)

    service = prepared_service(tmp_path, OversizedBackend())
    store = service.artifacts
    store.write_text(Path("CHARACTER_DEFINITION.md"), "stable character\n")
    store.write_text(Path("users/project-manager/USER_PERSONA.md"), "stable persona\n")

    with pytest.raises(WritebackValidationError, match="limit"):
        service.generate()

    assert store.read_text(Path("CHARACTER_DEFINITION.md")) == "stable character\n"
    assert (
        store.read_text(Path("users/project-manager/USER_PERSONA.md"))
        == "stable persona\n"
    )


def test_character_only_bootstrap_does_not_create_user_persona(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend()
    service = prepared_service(tmp_path, backend)

    version = service.generate_character()

    assert version.sha256
    assert service.artifacts.read_text(Path("CHARACTER_DEFINITION.md"))
    assert not service.artifacts.resolve(
        Path("users/project-manager/USER_PERSONA.md")
    ).exists()


def test_openai_writeback_uses_two_separate_structured_requests() -> None:
    class RoutingCompletions:
        def __init__(self) -> None:
            self.user_contents: list[str] = []

        def create(self, **kwargs: object) -> object:
            self.user_contents.append(kwargs["messages"][1]["content"])
            tool_name = kwargs["tools"][0]["function"]["name"]
            rendered = (
                "AI stays user-agnostic."
                if tool_name == "render_character_definition"
                else "User likes concise answers."
            )
            call = SimpleNamespace(
                function=SimpleNamespace(
                    name=tool_name,
                    arguments=json.dumps({"markdown": rendered}),
                )
            )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(tool_calls=[call], content=None)
                    )
                ]
            )

    completions = RoutingCompletions()
    backend = OpenAIWritebackBackend(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="agnes-model",
    )

    character = backend.render_character("decision rules", 100)
    persona = backend.render_user_persona("private user profile", 100)

    assert character == "AI stays user-agnostic."
    assert persona == "User likes concise answers."
    assert "private user profile" not in completions.user_contents[0]
    assert "decision rules" not in completions.user_contents[1]


def test_deterministic_persona_projection_covers_every_active_domain(
    tmp_path: Path,
) -> None:
    paths = resolve_scope(tmp_path, IDS)
    store = AtomicArtifactStore(paths.agent_root)
    store.write_text(
        Path("DECISION_RULES.md"),
        "# AI Decision Rules\n\n- Verify before acting.\n"
        "  - Evidence cards: card-1\n",
    )
    domains = (
        ("communication", "User prefers a concise conclusion before details."),
        (
            "bank_operation",
            "User requires independent verification for supplier payments.",
        ),
        ("security", "User refuses credential and verification-code sharing."),
        (
            "workflow",
            "User requires owners, deadlines, evidence, and dependencies.",
        ),
        (
            "crypto_investment",
            "User is a cryptocurrency beginner who prefers spot and low leverage.",
        ),
    )
    entries = [
        (
            f"<!-- dream-persona-id: persona-{index} -->\n"
            f"<!-- dream-persona-domain: {domain} -->\n"
            "<!-- dream-persona-confidence: 0.900 -->\n"
            f"{statement}\n"
            f"<!-- dream-source: evt-{index} -->"
        )
        for index, (domain, statement) in enumerate(domains, start=1)
    ]
    store.write_text(
        Path("users/project-manager/USER.md"),
        "\n§\n".join(entries) + "\n",
    )
    service = WritebackService(
        paths,
        backend=DeterministicWritebackBackend(),
        user_persona_limit=420,
    )

    service.generate()

    persona = store.read_text(Path("users/project-manager/USER_PERSONA.md"))
    for domain, _ in domains:
        assert f"[{domain}]" in persona
    assert "cryptocurrency beginner" in persona
    assert "spot and low leverage" in persona
    assert len(persona.strip()) <= 420


def test_projection_prioritizes_new_structured_persona_over_legacy_same_domain(
    tmp_path: Path,
) -> None:
    paths = resolve_scope(tmp_path, IDS)
    store = AtomicArtifactStore(paths.agent_root)
    store.write_text(
        Path("DECISION_RULES.md"),
        "# AI Decision Rules\n\n- Verify before acting.\n"
        "  - Evidence cards: card-1\n",
    )
    legacy = "Legacy crypto risk detail. " * 80
    structured = (
        "<!-- dream-persona-id: persona-crypto-new -->\n"
        "<!-- dream-persona-domain: crypto_investment -->\n"
        "<!-- dream-persona-confidence: 0.950 -->\n"
        "User is a cryptocurrency beginner who prefers spot and low leverage."
    )
    store.write_text(
        Path("users/project-manager/USER.md"),
        f"{legacy}\n<!-- dream-source: evt-old -->\n"
        "§\n"
        f"{structured}\n<!-- dream-source: evt-new -->\n",
    )
    service = WritebackService(
        paths,
        backend=DeterministicWritebackBackend(),
        user_persona_limit=180,
    )

    service.generate()

    persona = store.read_text(Path("users/project-manager/USER_PERSONA.md"))
    assert "[crypto_investment]" in persona
    assert "cryptocurrency beginner" in persona
    assert "spot and low leverage" in persona
