import json
from pathlib import Path

import pytest

from dream.curators.ai import AICurator
from dream.memory.publication import PublicationStore
from dream.extraction.models import ArtifactKind, ReviewAction, ReviewResult
from dream.core.scope import resolve_scope
from dream.application.service import DreamService
from dream.validation.seeds import (
    SeedSourceError,
    main,
    parse_seed_jsonl,
    seed_scope,
    validate_seed_file,
)
from dream.memory.writeback import DeterministicWritebackBackend, WritebackService


def seed_line(**overrides: object) -> str:
    record: dict[str, object] = {
        "source_dataset": "synthetic-helpfulness",
        "source_record_id": "helpful-001",
        "scenario": "Explain a deployment risk clearly.",
        "assistant_response": "State the conclusion, evidence, and next step.",
    }
    record.update(overrides)
    return json.dumps(record, ensure_ascii=False)


class SeedAwareBackend:
    def __init__(self) -> None:
        self.allowed_tools: frozenset[str] = frozenset()

    def review(self, request: object) -> ReviewResult:
        self.allowed_tools = request.allowed_tools
        source_event_id = request.event_id
        return ReviewResult(
            actions=(
                ReviewAction(
                    kind=ArtifactKind.USER_PROFILE,
                    tool_name="memory_manage",
                    payload={"action": "add", "content": "Seed user preference."},
                    source_event_id=source_event_id,
                ),
                ReviewAction(
                    kind=ArtifactKind.DECISION_CARD,
                    tool_name="decision_card_manage",
                    payload={
                        "id": "explain-risk-clearly",
                        "title": "清晰解释风险",
                        "scenario": "需要解释部署风险",
                        "signals": ["存在风险", "需要行动"],
                        "principle": "先给结论、证据和下一步。",
                        "outcome": "帮助用户做出判断。",
                        "boundaries": "不虚构证据。",
                        "confidence": 0.8,
                    },
                    source_event_id=source_event_id,
                ),
            ),
            summary="Seed review",
        )


def test_ai_seed_can_create_cards_but_never_user_profile(tmp_path: Path) -> None:
    backend = SeedAwareBackend()
    service = DreamService(tmp_path, backend=backend)

    result = service.import_ai_seed_jsonl(seed_line())
    runs = service.run_pending(seed_scope())
    paths = resolve_scope(tmp_path, seed_scope())

    assert result == {"imported": 1, "duplicates": 0}
    assert runs[0]["artifact_kinds"] == ["decision_card"]
    assert backend.allowed_tools == frozenset({"decision_card_manage"})
    assert list(paths.decision_cards_dir.glob("*.md"))
    assert not (paths.user_root / "USER.md").exists()
    assert PublicationStore(paths).pending_event_ids() == ()


def test_reimporting_the_same_seed_is_counted_as_a_duplicate(
    tmp_path: Path,
) -> None:
    service = DreamService(tmp_path, backend=SeedAwareBackend())

    first = service.import_ai_seed_jsonl(seed_line())
    second = service.import_ai_seed_jsonl(seed_line())

    assert first == {"imported": 1, "duplicates": 0}
    assert second == {"imported": 0, "duplicates": 1}
    assert len(service.ledger.read_all()) == 1


def test_ai_seed_builds_character_definition_without_user_persona(
    tmp_path: Path,
) -> None:
    service = DreamService(tmp_path, backend=SeedAwareBackend())
    service.import_ai_seed_jsonl(seed_line())
    service.run_pending(seed_scope())
    paths = resolve_scope(tmp_path, seed_scope())

    AICurator(paths).run()
    WritebackService(
        paths,
        backend=DeterministicWritebackBackend(),
    ).generate_character()

    assert (paths.agent_root / "CHARACTER_DEFINITION.md").exists()
    assert not (paths.user_root / "USER.md").exists()
    assert not (paths.user_root / "USER_PERSONA.md").exists()


def test_seed_parser_rejects_hidden_or_user_profile_fields() -> None:
    with pytest.raises(SeedSourceError):
        parse_seed_jsonl(seed_line(hidden_persona={"role": "secret"}))
    with pytest.raises(SeedSourceError):
        parse_seed_jsonl(seed_line(user_profile="must not enter seed input"))


def test_seed_parser_requires_unique_dataset_record_ids() -> None:
    duplicate = seed_line() + "\n" + seed_line()

    with pytest.raises(SeedSourceError, match="duplicate"):
        parse_seed_jsonl(duplicate)


def test_seed_file_validation_and_cli_require_exact_count(tmp_path: Path) -> None:
    path = tmp_path / "seed.jsonl"
    path.write_text(
        seed_line()
        + "\n"
        + seed_line(source_record_id="safe-001", source_dataset="synthetic-safety")
        + "\n",
        encoding="utf-8",
    )

    assert validate_seed_file(path, expected_count=2) == 2
    assert main(["validate", str(path), "--expected-count", "2"]) == 0
    with pytest.raises(SeedSourceError, match="expected 3"):
        validate_seed_file(path, expected_count=3)


def test_generated_seed_fixture_contains_two_valid_synthetic_records(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sample.jsonl"
    path.write_text(
        seed_line()
        + "\n"
        + seed_line(
            source_dataset="synthetic-safety",
            source_record_id="boundary-001",
        )
        + "\n",
        encoding="utf-8",
    )

    records = parse_seed_jsonl(path.read_text(encoding="utf-8"))

    assert len(records) == 2
    assert all(record.source_dataset.startswith("synthetic-") for record in records)
