import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from dream.validation.campaign import (
    CampaignBlocked,
    CampaignPhase,
    CampaignValidationError,
    CodexCampaignStore,
    CodexTaskReceipt,
)


PROFILE_SHA = "a" * 64
EMPTY_CONTEXT_SHA = hashlib.sha256(b"").hexdigest()
EVOLVED_CONTEXT_SHA = "b" * 64
USERS = ("project-manager", "python-beginner", "technical-lead")


def receipt(
    user_id: str,
    number: int,
    *,
    thread_id: str | None = None,
    event_id: str | None = None,
    profile_sha256: str = PROFILE_SHA,
    version: int | None = None,
) -> CodexTaskReceipt:
    if version is None:
        version = 0 if number <= 5 else (1 if number <= 10 else 2)
    return CodexTaskReceipt(
        user_id=user_id,
        task_number=number,
        event_id=event_id or f"event-{user_id}-{number}",
        thread_id=thread_id or f"thread-{user_id}-{number}",
        profile_sha256=profile_sha256,
        dream_context_sha256=(
            EMPTY_CONTEXT_SHA if number <= 5 else EVOLVED_CONTEXT_SHA
        ),
        active_publication_version=version,
    )


def record_range(
    store: CodexCampaignStore,
    user_id: str,
    start: int,
    end: int,
) -> None:
    for number in range(start, end + 1):
        store.record(receipt(user_id, number))


def state_after_ten_tasks(root: Path, user_id: str) -> CodexCampaignStore:
    store = CodexCampaignStore(root, approved_profile_sha=PROFILE_SHA)
    record_range(store, user_id, 1, 5)
    store.note_active_cycle(user_id, cycle=1, version=1)
    record_range(store, user_id, 6, 10)
    return store


def test_receipt_derives_phase_and_rejects_baseline_context() -> None:
    assert receipt("project-manager", 1).phase is CampaignPhase.BASELINE
    assert receipt("project-manager", 6).phase is CampaignPhase.EVOLVED_V1
    assert receipt("project-manager", 11).phase is CampaignPhase.EVOLVED_V2

    with pytest.raises(ValidationError, match="baseline"):
        CodexTaskReceipt(
            **{
                **receipt("project-manager", 1).model_dump(),
                "active_publication_version": 1,
            }
        )


def test_task_six_waits_for_first_active_dream(tmp_path: Path) -> None:
    store = CodexCampaignStore(tmp_path, approved_profile_sha=PROFILE_SHA)
    record_range(store, "project-manager", 1, 5)

    with pytest.raises(CampaignBlocked, match="cycle 1"):
        store.assert_can_create("project-manager", 6)

    store.note_active_cycle("project-manager", cycle=1, version=1)
    store.assert_can_create("project-manager", 6)


def test_task_eleven_waits_for_second_active_dream(tmp_path: Path) -> None:
    store = state_after_ten_tasks(tmp_path, "python-beginner")

    with pytest.raises(CampaignBlocked, match="cycle 2"):
        store.assert_can_create("python-beginner", 11)

    store.note_active_cycle("python-beginner", cycle=2, version=2)
    store.assert_can_create("python-beginner", 11)


def test_thread_id_and_event_id_are_globally_unique(tmp_path: Path) -> None:
    store = CodexCampaignStore(tmp_path, approved_profile_sha=PROFILE_SHA)
    store.record(receipt("project-manager", 1, thread_id="thread-one"))

    with pytest.raises(CampaignValidationError, match="thread ID"):
        store.record(receipt("technical-lead", 1, thread_id="thread-one"))
    with pytest.raises(CampaignValidationError, match="event ID"):
        store.record(receipt("technical-lead", 1, event_id="event-project-manager-1"))


def test_store_rejects_skipped_task_and_changed_profile(tmp_path: Path) -> None:
    store = CodexCampaignStore(tmp_path, approved_profile_sha=PROFILE_SHA)

    with pytest.raises(CampaignBlocked, match="expected task 1"):
        store.record(receipt("project-manager", 2))
    with pytest.raises(CampaignValidationError, match="profile hash"):
        store.record(
            receipt("project-manager", 1, profile_sha256="c" * 64)
        )


def test_campaign_recovers_receipts_and_cycles_after_restart(tmp_path: Path) -> None:
    store = CodexCampaignStore(tmp_path, approved_profile_sha=PROFILE_SHA)
    record_range(store, "technical-lead", 1, 5)
    store.note_active_cycle("technical-lead", cycle=1, version=3)

    restarted = CodexCampaignStore(tmp_path, approved_profile_sha=PROFILE_SHA)

    restarted.assert_can_create("technical-lead", 6)
    restarted.record(receipt("technical-lead", 6, version=3))


def test_summary_requires_exactly_36_tasks_and_two_cycles_each(
    tmp_path: Path,
) -> None:
    store = CodexCampaignStore(tmp_path, approved_profile_sha=PROFILE_SHA)
    for user_id in USERS:
        record_range(store, user_id, 1, 5)
        store.note_active_cycle(user_id, cycle=1, version=1)
        record_range(store, user_id, 6, 10)
        store.note_active_cycle(user_id, cycle=2, version=2)
        record_range(store, user_id, 11, 12)

    summary = store.summary()

    assert summary["task_count"] == 36
    assert summary["unique_thread_count"] == 36
    assert summary["users"] == {
        user_id: {"task_count": 12, "active_cycles": 2} for user_id in USERS
    }


def test_summary_blocks_incomplete_campaign(tmp_path: Path) -> None:
    store = CodexCampaignStore(tmp_path, approved_profile_sha=PROFILE_SHA)
    store.record(receipt("project-manager", 1))

    with pytest.raises(CampaignBlocked, match="incomplete"):
        store.summary()
