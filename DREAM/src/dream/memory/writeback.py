"""Generate bounded, separately scoped Character.AI writeback artifacts."""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Protocol

from dream.memory.artifacts import ArtifactVersion, AtomicArtifactStore
from dream.memory.writeback_prompts import (
    CHARACTER_WRITEBACK_PROMPT,
    USER_PERSONA_WRITEBACK_PROMPT,
)
from dream.governance.persona_models import parse_persona_atom
from dream.memory.items import parse_memory_items
from dream.memory.storage.rollback import RollbackService
from dream.core.scope import ScopePaths
from dream.extraction.structured import StructuredCompletionClient


class WritebackBackend(Protocol):
    def render_character(self, decision_rules: str, limit: int) -> str: ...

    def render_user_persona(self, user_profile: str, limit: int) -> str: ...


class WritebackValidationError(ValueError):
    """Candidate writeback failed local validation."""


@dataclass(frozen=True)
class WritebackArtifacts:
    character: ArtifactVersion
    user_persona: ArtifactVersion
    rollback_snapshot_id: str


_CHARACTER_TOOL = {
    "type": "function",
    "function": {
        "name": "render_character_definition",
        "parameters": {
            "type": "object",
            "properties": {"markdown": {"type": "string"}},
            "required": ["markdown"],
            "additionalProperties": False,
        },
    },
}

_USER_TOOL = {
    "type": "function",
    "function": {
        "name": "render_user_persona",
        "parameters": {
            "type": "object",
            "properties": {"markdown": {"type": "string"}},
            "required": ["markdown"],
            "additionalProperties": False,
        },
    },
}


class OpenAIWritebackBackend:
    def __init__(
        self,
        *,
        client: Any,
        model: str,
        structured_mode: str = "auto",
        max_completion_tokens: int = 2000,
    ) -> None:
        self.structured_mode = structured_mode
        self.structured = StructuredCompletionClient(
            client,
            model,
            max_completion_tokens=max_completion_tokens,
        )

    def _render(
        self,
        *,
        prompt: str,
        source: str,
        limit: int,
        tool: dict[str, object],
    ) -> str:
        name = str(tool["function"]["name"])
        calls = self.structured.call(
            system=prompt,
            content=f"<limit>{limit}</limit>\n<source>\n{source}\n</source>",
            tools=(tool,),
            forced_tool=name,
            mode=self.structured_mode,
        )
        return str(calls[0].arguments["markdown"])

    def render_character(self, decision_rules: str, limit: int) -> str:
        return self._render(
            prompt=CHARACTER_WRITEBACK_PROMPT,
            source=decision_rules,
            limit=limit,
            tool=_CHARACTER_TOOL,
        )

    def render_user_persona(self, user_profile: str, limit: int) -> str:
        return self._render(
            prompt=USER_PERSONA_WRITEBACK_PROMPT,
            source=user_profile,
            limit=limit,
            tool=_USER_TOOL,
        )


class DeterministicWritebackBackend:
    def render_character(self, decision_rules: str, limit: int) -> str:
        return decision_rules.strip()[:limit]

    def render_user_persona(self, user_profile: str, limit: int) -> str:
        return user_profile.strip()[:limit]


class PersonaProjection:
    """Build a bounded view that represents every active persona domain."""

    minimum_confidence = 0.7

    def render(self, repository: str, limit: int) -> str | None:
        if "<!-- dream-persona-domain:" not in repository:
            return None
        grouped: dict[str, list[str]] = {}
        for item in parse_memory_items(repository):
            atom = parse_persona_atom(item.content)
            if atom.confidence < self.minimum_confidence or not atom.statement:
                continue
            statements = grouped.setdefault(atom.domain, [])
            normalized = " ".join(atom.statement.split())
            if normalized not in statements:
                if "<!-- dream-persona-domain:" in item.content:
                    statements.insert(0, normalized)
                else:
                    statements.append(normalized)
        if not grouped:
            return None

        header = "# User Persona\n\n"
        lines = [
            f"- [{domain}] {'; '.join(statements)}"
            for domain, statements in grouped.items()
        ]
        rendered = header + "\n".join(lines)
        if len(rendered) <= limit:
            return rendered

        separators = len(lines) - 1
        fixed = len(header) + separators + sum(
            len(f"- [{domain}] ") for domain in grouped
        )
        available = limit - fixed
        if available < len(lines):
            raise WritebackValidationError(
                "User Persona limit cannot represent every active domain"
            )
        base, remainder = divmod(available, len(lines))
        bounded: list[str] = []
        for index, (domain, statements) in enumerate(grouped.items()):
            allowance = base + int(index < remainder)
            statement = "; ".join(statements)
            bounded.append(
                f"- [{domain}] {self._excerpt(statement, allowance)}"
            )
        return header + "\n".join(bounded)

    @staticmethod
    def _excerpt(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        if limit <= 1:
            return value[:limit]
        return value[: limit - 1].rstrip() + "…"


class WritebackService:
    def __init__(
        self,
        paths: ScopePaths,
        *,
        backend: WritebackBackend,
        character_limit: int = 3200,
        user_persona_limit: int = 1200,
    ) -> None:
        self.paths = paths
        self.backend = backend
        self.character_limit = character_limit
        self.user_persona_limit = user_persona_limit
        self.artifacts = AtomicArtifactStore(paths.agent_root)
        self.rollback = RollbackService(paths)
        self.persona_projection = PersonaProjection()

    def _validate(self, value: str, limit: int, label: str) -> str:
        rendered = value.strip()
        if not rendered or "\x00" in rendered:
            raise WritebackValidationError(f"{label} must be non-empty text")
        if len(rendered) > limit:
            raise WritebackValidationError(f"{label} exceeds its limit")
        return rendered + "\n"

    def _character_candidate(self) -> str:
        rules = self.artifacts.read_text(Path("DECISION_RULES.md"))
        if "Evidence cards:" not in rules:
            raise WritebackValidationError("decision rules lack evidence cards")
        return self._validate(
            self.backend.render_character(rules, self.character_limit),
            self.character_limit,
            "Character Definition",
        )

    def generate_character(self) -> ArtifactVersion:
        candidate = self._character_candidate()
        relative = Path("CHARACTER_DEFINITION.md")
        self.rollback.capture((relative,))
        return self.artifacts.write_text(relative, candidate)

    def generate(self) -> WritebackArtifacts:
        character = self._character_candidate()
        user_relative = Path("users") / self.paths.user_root.name / "USER.md"
        profile = self.artifacts.read_text(user_relative)
        if "dream-source" not in profile:
            raise WritebackValidationError("user profile lacks source evidence")
        public_profile = re.sub(
            r"\n?<!-- dream-sources?:\s*[^>]+ -->",
            "",
            profile,
        ).strip()
        backend_persona = self._validate(
            self.backend.render_user_persona(public_profile, self.user_persona_limit),
            self.user_persona_limit,
            "User Persona",
        )
        projected = self.persona_projection.render(
            public_profile,
            self.user_persona_limit,
        )
        persona = self._validate(
            projected if projected is not None else backend_persona,
            self.user_persona_limit,
            "User Persona",
        )
        character_relative = Path("CHARACTER_DEFINITION.md")
        persona_relative = Path("users") / self.paths.user_root.name / "USER_PERSONA.md"
        snapshot_id = self.rollback.capture((character_relative, persona_relative))
        character_version = self.artifacts.write_text(character_relative, character)
        persona_version = self.artifacts.write_text(persona_relative, persona)
        return WritebackArtifacts(
            character=character_version,
            user_persona=persona_version,
            rollback_snapshot_id=snapshot_id,
        )
