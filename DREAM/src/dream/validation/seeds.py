"""Strict AI-only seed ingestion and validation CLI."""

import argparse
from pathlib import Path
from typing import Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from dream.core.events import TaskCompletedEvent
from dream.core.scope import ScopeIds


class AISeedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_dataset: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    assistant_response: str = Field(min_length=1)

    @field_validator(
        "source_dataset",
        "source_record_id",
        "scenario",
        "assistant_response",
    )
    @classmethod
    def value_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("seed text must not be blank")
        return value


class SeedSourceError(ValueError):
    """A seed file violates the AI-only source contract."""


def seed_scope() -> ScopeIds:
    return ScopeIds("dream-lab", "enterprise-colleague", "seed-only")


def parse_seed_jsonl(text: str) -> tuple[AISeedRecord, ...]:
    records: list[AISeedRecord] = []
    identities: set[tuple[str, str]] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = AISeedRecord.model_validate_json(line)
        except ValidationError as exc:
            reason = str(
                exc.errors(include_input=False)[0].get("msg", "seed validation failed")
            )
            raise SeedSourceError(
                f"invalid AI seed record at line {line_number}: {reason}"
            ) from exc
        identity = (record.source_dataset, record.source_record_id)
        if identity in identities:
            raise SeedSourceError(f"duplicate AI seed ID at line {line_number}")
        identities.add(identity)
        records.append(record)
    return tuple(records)


def seed_record_to_event(record: AISeedRecord) -> TaskCompletedEvent:
    event_id = f"ai-seed:{record.source_dataset}:{record.source_record_id}"
    return TaskCompletedEvent(
        event_id=event_id,
        task_id=event_id,
        scope=seed_scope(),
        completed_at="1970-01-01T00:00:00+00:00",
        interrupted=False,
        tool_iterations=10,
        transcript=(
            {"role": "user", "content": record.scenario},
            {"role": "assistant", "content": record.assistant_response},
        ),
        final_response=record.assistant_response,
        source_refs=(
            {
                "source": "ai-seed",
                "source_dataset": record.source_dataset,
                "source_record_id": record.source_record_id,
            },
        ),
    )


def validate_seed_file(path: Path, expected_count: int) -> int:
    records = parse_seed_jsonl(path.read_text(encoding="utf-8"))
    if len(records) != expected_count:
        raise SeedSourceError(
            f"expected {expected_count} AI seed records, found {len(records)}"
        )
    return len(records)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate DREAM AI-only seeds")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate a JSONL seed file")
    validate.add_argument("path", type=Path)
    validate.add_argument("--expected-count", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate":
        count = validate_seed_file(args.path, args.expected_count)
        print(f"{count} valid AI-only seed records; 0 user-profile fields")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
