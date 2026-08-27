import json
from pathlib import Path

import pytest

from dream.integrations.manual import parse_manual_ndjson
from dream.validation.evaluation import (
    EvaluationReportError,
    UserEvaluationInput,
    ValidationRunInput,
    evaluate_validation_run,
    main,
    verify_report,
)


def user(
    user_id: str,
    *,
    task_count: int = 12,
    active_dream_cycles: int = 2,
) -> UserEvaluationInput:
    return UserEvaluationInput(
        user_id=user_id,
        task_count=task_count,
        active_dream_cycles=active_dream_cycles,
        supported_profile_facts=17,
        total_profile_facts=20,
        personalized_successes=8,
        personalized_tasks=10,
    )


def passing_run(**overrides: object) -> ValidationRunInput:
    values: dict[str, object] = {
        "users": [
            user("project-manager"),
            user("python-beginner"),
            user("technical-lead"),
        ],
        "severe_hallucinations": 0,
        "cross_user_leaks": 0,
        "evolved_ai_successes": 8,
        "evolved_ai_tasks": 10,
        "completed_dream_writeback_cycles": 6,
        "change_conflict_case_passed": True,
        "failure_fallback_or_rollback_passed": True,
        "missing_source_event_ids": 0,
        "incomplete_writebacks": 0,
        "inactive_publications": 0,
        "decision_cards_with_private_user_data": 0,
        "agent_profile_version": 1,
        "agent_profile_sha256_before": "a" * 64,
        "agent_profile_sha256_after": "a" * 64,
        "codex_thread_count": 36,
        "missing_codex_threads": 0,
        "duplicate_codex_threads": 0,
        "agnes_advisory": "advisory only",
    }
    values.update(overrides)
    return ValidationRunInput.model_validate(values)


def test_acceptance_requires_evidence_personalization_and_zero_leakage() -> None:
    report = evaluate_validation_run(passing_run())

    assert report.profile_evidence_rate == 0.85
    assert report.personalization_rate == 0.80
    assert report.ai_evolution_rate == 0.80
    assert report.passed is True


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("severe_hallucinations", 1, "severe hallucinations"),
        ("cross_user_leaks", 1, "cross-user leaks"),
        ("missing_source_event_ids", 1, "missing source event IDs"),
        ("incomplete_writebacks", 1, "incomplete writebacks"),
        ("inactive_publications", 1, "inactive publications"),
        (
            "decision_cards_with_private_user_data",
            1,
            "private user data in decision cards",
        ),
        ("completed_dream_writeback_cycles", 1, "dream/writeback cycles"),
        ("change_conflict_case_passed", False, "preference-change case"),
        (
            "failure_fallback_or_rollback_passed",
            False,
            "failure fallback or rollback",
        ),
    ],
)
def test_any_structural_or_safety_failure_blocks_acceptance(
    field: str, value: object, reason: str
) -> None:
    report = evaluate_validation_run(passing_run(**{field: value}))

    assert report.passed is False
    assert any(reason in item for item in report.failure_reasons)


def test_any_user_without_exactly_twelve_tasks_fails_acceptance() -> None:
    users = [
        user("project-manager"),
        user("python-beginner", task_count=9),
        user("technical-lead"),
    ]

    report = evaluate_validation_run(passing_run(users=users))

    assert report.passed is False
    assert any("python-beginner" in item for item in report.failure_reasons)


def test_acceptance_requires_36_real_codex_tasks() -> None:
    report = evaluate_validation_run(passing_run(codex_thread_count=35))

    assert report.passed is False
    assert any("36 Codex" in reason for reason in report.failure_reasons)


def test_acceptance_requires_unchanged_initial_profile() -> None:
    report = evaluate_validation_run(
        passing_run(agent_profile_sha256_after="b" * 64)
    )

    assert report.passed is False
    assert any("profile hash" in reason for reason in report.failure_reasons)


@pytest.mark.parametrize(
    "field,reason",
    [
        ("missing_codex_threads", "missing Codex"),
        ("duplicate_codex_threads", "duplicate Codex"),
    ],
)
def test_missing_or_duplicate_codex_thread_fails(
    field: str,
    reason: str,
) -> None:
    report = evaluate_validation_run(passing_run(**{field: 1}))

    assert report.passed is False
    assert any(reason in item for item in report.failure_reasons)


def test_each_user_requires_two_active_dream_cycles() -> None:
    users = [
        user("project-manager"),
        user("python-beginner", active_dream_cycles=1),
        user("technical-lead"),
    ]

    report = evaluate_validation_run(passing_run(users=users))

    assert report.passed is False
    assert any("python-beginner" in item for item in report.failure_reasons)


def test_public_seed_data_is_not_an_acceptance_input() -> None:
    assert "public_seed_count" not in ValidationRunInput.model_fields


def test_agnes_advisory_cannot_override_failed_human_evidence() -> None:
    weak = user("project-manager").model_copy(update={"supported_profile_facts": 0})
    report = evaluate_validation_run(
        passing_run(
            users=[
                weak,
                user("python-beginner"),
                user("technical-lead"),
            ],
            agnes_advisory="Agnes says everything passed",
        )
    )

    assert report.profile_evidence_rate < 0.85
    assert report.passed is False


def test_saved_report_is_recomputed_and_cli_returns_acceptance_status(
    tmp_path: Path,
) -> None:
    passed_path = tmp_path / "passed.json"
    failed_path = tmp_path / "failed.json"
    passed_path.write_text(
        evaluate_validation_run(passing_run()).model_dump_json(indent=2),
        encoding="utf-8",
    )
    failed_path.write_text(
        evaluate_validation_run(passing_run(cross_user_leaks=1)).model_dump_json(
            indent=2
        ),
        encoding="utf-8",
    )

    assert verify_report(passed_path).passed is True
    assert main(["verify", str(passed_path)]) == 0
    assert main(["verify", str(failed_path)]) == 1


def test_tampered_computed_rates_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "tampered.json"
    payload = json.loads(evaluate_validation_run(passing_run()).model_dump_json())
    payload["profile_evidence_rate"] = 1.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvaluationReportError, match="does not match"):
        verify_report(path)


def test_generated_conversations_are_valid_but_below_task_threshold(
    tmp_path: Path,
) -> None:
    root = tmp_path / "conversations"
    root.mkdir()
    identities = (
        ("project_manager", "project-manager"),
        ("python_beginner", "python-beginner"),
        ("technical_lead", "technical-lead"),
    )
    for name, user_id in identities:
        records = []
        for index in (1, 2):
            response = f"Safe synthetic response {index} for {user_id}."
            records.append(
                json.dumps(
                    {
                        "event_id": f"{name}-{index}",
                        "tenant_id": "dream-lab",
                        "agent_id": "enterprise-colleague",
                        "user_id": user_id,
                        "session_id": f"{name}-session-{index}",
                        "task_id": f"{name}-task-{index}",
                        "completed_at": f"2026-07-17T1{index}:00:00+08:00",
                        "messages": [
                            {
                                "role": "user",
                                "content": f"Synthetic request {index} for {user_id}.",
                            },
                            {"role": "assistant", "content": response},
                        ],
                        "final_response": response,
                    },
                    ensure_ascii=False,
                )
            )
        (root / f"{name}.jsonl").write_text(
            "\n".join(records) + "\n",
            encoding="utf-8",
        )

    users = []
    for name in ("project_manager", "python_beginner", "technical_lead"):
        records = parse_manual_ndjson(
            (root / f"{name}.jsonl").read_text(encoding="utf-8")
        )
        assert len(records) == 2
        users.append(user(records[0].user_id, task_count=len(records)))

    report = evaluate_validation_run(passing_run(users=users))

    assert report.passed is False
    assert sum(item.task_count for item in report.run.users) == 6
