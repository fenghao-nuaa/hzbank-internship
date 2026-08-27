"""Recomputable acceptance report for closed-loop evolution validation."""

import argparse
from pathlib import Path
import sys
from typing import Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)


class UserEvaluationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str = Field(min_length=1)
    task_count: int = Field(ge=0)
    active_dream_cycles: int = Field(ge=0)
    supported_profile_facts: int = Field(ge=0)
    total_profile_facts: int = Field(ge=0)
    personalized_successes: int = Field(ge=0)
    personalized_tasks: int = Field(ge=0)

    @model_validator(mode="after")
    def counts_are_consistent(self) -> "UserEvaluationInput":
        if self.supported_profile_facts > self.total_profile_facts:
            raise ValueError("supported profile facts exceed total facts")
        if self.personalized_successes > self.personalized_tasks:
            raise ValueError("personalized successes exceed evaluated tasks")
        return self


class ValidationRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    users: tuple[UserEvaluationInput, ...] = Field(min_length=1)
    severe_hallucinations: int = Field(ge=0)
    cross_user_leaks: int = Field(ge=0)
    evolved_ai_successes: int = Field(ge=0)
    evolved_ai_tasks: int = Field(ge=0)
    completed_dream_writeback_cycles: int = Field(ge=0)
    change_conflict_case_passed: bool
    failure_fallback_or_rollback_passed: bool
    missing_source_event_ids: int = Field(ge=0)
    incomplete_writebacks: int = Field(ge=0)
    inactive_publications: int = Field(ge=0)
    decision_cards_with_private_user_data: int = Field(ge=0)
    agent_profile_version: int = Field(ge=1)
    agent_profile_sha256_before: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_profile_sha256_after: str = Field(pattern=r"^[0-9a-f]{64}$")
    codex_thread_count: int = Field(ge=0)
    missing_codex_threads: int = Field(ge=0)
    duplicate_codex_threads: int = Field(ge=0)
    agnes_advisory: str = ""

    @model_validator(mode="after")
    def run_counts_are_consistent(self) -> "ValidationRunInput":
        if self.evolved_ai_successes > self.evolved_ai_tasks:
            raise ValueError("AI successes exceed evaluated tasks")
        identities = [item.user_id for item in self.users]
        if len(identities) != len(set(identities)):
            raise ValueError("evaluation user IDs must be unique")
        return self


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run: ValidationRunInput
    profile_evidence_rate: float
    personalization_rate: float
    ai_evolution_rate: float
    passed: bool
    failure_reasons: tuple[str, ...]


class EvaluationReportError(ValueError):
    """A saved evaluation report is malformed or was not reproducible."""


def _rate(successes: int, total: int) -> float:
    return successes / total if total else 0.0


def evaluate_validation_run(run: ValidationRunInput) -> EvaluationReport:
    supported = sum(item.supported_profile_facts for item in run.users)
    profile_facts = sum(item.total_profile_facts for item in run.users)
    personalized = sum(item.personalized_successes for item in run.users)
    personalized_tasks = sum(item.personalized_tasks for item in run.users)
    profile_rate = _rate(supported, profile_facts)
    personalization_rate = _rate(personalized, personalized_tasks)
    ai_rate = _rate(run.evolved_ai_successes, run.evolved_ai_tasks)

    failures: list[str] = []
    if profile_rate < 0.85:
        failures.append("profile evidence rate is below 0.85")
    if run.severe_hallucinations:
        failures.append("severe hallucinations must be zero")
    if run.cross_user_leaks:
        failures.append("cross-user leaks must be zero")
    if personalization_rate < 0.80:
        failures.append("personalization rate is below 0.80")
    if ai_rate < 0.80:
        failures.append("AI evolution rate is below 0.80")
    expected_users = {"project-manager", "python-beginner", "technical-lead"}
    actual_users = {item.user_id for item in run.users}
    if actual_users != expected_users:
        failures.append("exactly three named validation users are required")
    for item in run.users:
        if item.task_count != 12:
            failures.append(f"user {item.user_id} must have exactly 12 tasks")
        if item.active_dream_cycles < 2:
            failures.append(f"user {item.user_id} requires two active dream cycles")
    if run.completed_dream_writeback_cycles < 6:
        failures.append("at least six dream/writeback cycles are required")
    if not run.change_conflict_case_passed:
        failures.append("preference-change case did not pass")
    if not run.failure_fallback_or_rollback_passed:
        failures.append("failure fallback or rollback did not pass")
    if run.missing_source_event_ids:
        failures.append("missing source event IDs must be zero")
    if run.incomplete_writebacks:
        failures.append("incomplete writebacks must be zero")
    if run.inactive_publications:
        failures.append("inactive publications must be zero")
    if run.decision_cards_with_private_user_data:
        failures.append("private user data in decision cards must be zero")
    if run.agent_profile_sha256_before != run.agent_profile_sha256_after:
        failures.append("approved Agent profile hash changed during validation")
    if run.codex_thread_count != 36:
        failures.append("exactly 36 Codex tasks are required")
    if run.missing_codex_threads:
        failures.append("missing Codex threads must be zero")
    if run.duplicate_codex_threads:
        failures.append("duplicate Codex threads must be zero")

    return EvaluationReport(
        run=run,
        profile_evidence_rate=profile_rate,
        personalization_rate=personalization_rate,
        ai_evolution_rate=ai_rate,
        passed=not failures,
        failure_reasons=tuple(failures),
    )


def verify_report(path: Path) -> EvaluationReport:
    try:
        saved = EvaluationReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise EvaluationReportError("evaluation report is invalid") from exc
    computed = evaluate_validation_run(saved.run)
    compared = (
        "profile_evidence_rate",
        "personalization_rate",
        "ai_evolution_rate",
        "passed",
        "failure_reasons",
    )
    if any(getattr(saved, field) != getattr(computed, field) for field in compared):
        raise EvaluationReportError(
            "saved evaluation result does not match recomputed metrics"
        )
    return computed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a DREAM evaluation report")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="recompute and verify a report")
    verify.add_argument("path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "verify":
        return 2
    try:
        report = verify_report(args.path)
    except EvaluationReportError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        "profile_evidence_rate="
        f"{report.profile_evidence_rate:.2f} "
        f"personalization_rate={report.personalization_rate:.2f} "
        f"ai_evolution_rate={report.ai_evolution_rate:.2f} "
        f"codex_threads={report.run.codex_thread_count} "
        "profile_unchanged="
        f"{str(report.run.agent_profile_sha256_before == report.run.agent_profile_sha256_after).lower()} "
        f"passed={str(report.passed).lower()}"
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
