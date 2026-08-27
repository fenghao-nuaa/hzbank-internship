from pathlib import Path
import json

import httpx
import pytest

from dream.api import create_app
from dream.memory.writeback import DeterministicWritebackBackend


def _manual_line(**overrides: object) -> str:
    record: dict[str, object] = {
        "event_id": "evt-project-1",
        "tenant_id": "dream-lab",
        "agent_id": "enterprise-colleague",
        "user_id": "project-manager",
        "session_id": "session-1",
        "task_id": "task-1",
        "completed_at": "2026-07-17T10:00:00+08:00",
        "messages": [
            {"role": "user", "content": "先告诉我结论。"},
            {"role": "assistant", "content": "结论：还需要接口联调。"},
        ],
        "final_response": "结论：还需要接口联调。",
    }
    record.update(overrides)
    return json.dumps(record, ensure_ascii=False)


@pytest.mark.asyncio
async def test_manual_ndjson_api_imports_once_and_reports_duplicate(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://dream.test"
    ) as client:
        first = await client.post(
            "/v1/validation/import",
            content=_manual_line() + "\n",
            headers={"Content-Type": "application/x-ndjson"},
        )
        second = await client.post(
            "/v1/validation/import",
            content=_manual_line() + "\n",
            headers={"Content-Type": "application/x-ndjson"},
        )

    assert first.status_code == 200
    assert first.json() == {"imported": 1, "duplicates": 0}
    assert second.json() == {"imported": 0, "duplicates": 1}
    assert len(app.state.dream_service.ledger.read_all()) == 1


@pytest.mark.asyncio
async def test_manual_ndjson_api_rejects_hidden_persona_without_echoing_it(
    tmp_path: Path,
) -> None:
    secret = "hidden-manager-secret"
    app = create_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://dream.test"
    ) as client:
        response = await client.post(
            "/v1/validation/import",
            content=_manual_line(hidden_persona={"secret": secret}),
            headers={"Content-Type": "application/x-ndjson"},
        )

    assert response.status_code == 422
    assert secret not in response.text


@pytest.mark.asyncio
async def test_conversation_dream_updates_only_next_context_and_current_user(
    tmp_path: Path,
) -> None:
    transport = httpx.ASGITransport(app=create_app(tmp_path))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://dream.test"
    ) as client:
        scope = {
            "tenant_id": "acme",
            "agent_id": "assistant",
            "user_id": "alice",
        }

        first = (await client.post("/v1/tasks/start", json=scope)).json()
        queued = await client.post(
            "/v1/dream/conversations",
            json={
                **scope,
                "event_id": "evt-1",
                "conversation_id": "conversation-1",
                "completed_at": "2026-07-15T10:00:00+08:00",
                "interrupted": False,
                "tool_iterations": 12,
                "headroom_summary": "The user values concise communication.",
                "messages": [
                    {"role": "user", "content": "I prefer concise answers"},
                    {
                        "role": "assistant",
                        "content": (
                            "Assistant decision: verify before risky action "
                            "because it is irreversible"
                        ),
                    },
                ],
                "final_response": "Verified before applying the change.",
            },
        )
        assert queued.status_code == 202

        run = await client.post("/v1/dream/run-pending")
        assert run.status_code == 200
        assert run.json()["runs"][0]["status"] == "active"
        assert run.json()["runs"][0]["source_event_ids"] == ["evt-1"]

        second = (await client.post("/v1/tasks/start", json=scope)).json()
        bob = (
            await client.post(
                "/v1/tasks/start",
                json={
                    "tenant_id": "acme",
                    "agent_id": "assistant",
                    "user_id": "bob",
                },
            )
        ).json()

        assert first["snapshot_id"] != second["snapshot_id"]
        assert "Prefers concise answers" not in first["user_profile"]
        assert "Prefers concise answers" in second["user_profile"]
        assert any("高风险操作前先验证" in card for card in second["decision_cards"])
        assert "Prefers concise answers" not in bob["user_profile"]

        curated = await client.post("/v1/dream/run-curators", json=scope)
        assert curated.status_code == 200
        after_curator = (await client.post("/v1/tasks/start", json=scope)).json()
        assert "先完成只读验证，再决定是否执行。" in after_curator["decision_rules"]


@pytest.mark.asyncio
async def test_validation_api_blocks_next_task_until_dream_is_active(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_VALIDATION_REQUIRE_ACTIVE_WRITEBACK=true\n",
        encoding="utf-8",
    )
    app = create_app(tmp_path, env_file=env_file)
    transport = httpx.ASGITransport(app=app)
    scope = {
        "tenant_id": "dream-lab",
        "agent_id": "enterprise-colleague",
        "user_id": "project-manager",
    }
    async with httpx.AsyncClient(
        transport=transport, base_url="http://dream.test"
    ) as client:
        imported = await client.post(
            "/v1/validation/import",
            content=_manual_line(
                messages=[
                    {"role": "user", "content": "I prefer concise answers"},
                    {
                        "role": "assistant",
                        "content": (
                            "Always verify before risky action; verified "
                            "before applying the change."
                        ),
                    },
                ],
                final_response=(
                    "Always verify before risky action; verified before "
                    "applying the change."
                ),
            )
            + "\n",
            headers={"Content-Type": "application/x-ndjson"},
        )
        blocked = await client.post("/v1/tasks/start", json=scope)
        candidate = await client.post("/v1/validation/dream", json=scope)
        assert candidate.status_code == 200, candidate.text
        version = candidate.json()["version"]
        status_before = await client.get(
            "/v1/validation/publications/status",
            params=scope,
        )
        redundant_approve = await client.post(
            f"/v1/validation/publications/{version}/approve", json=scope
        )
        started = await client.post("/v1/tasks/start", json=scope)

    assert imported.status_code == 200
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {
        "latest_completed_event_id": "evt-project-1",
        "active_processed_through_event_id": "",
        "next_action": "complete and activate the pending dream publication",
    }
    assert candidate.json()["status"] == "active"
    assert status_before.json()["active"]["version"] == version
    assert status_before.json()["latest"]["version"] == version
    assert redundant_approve.status_code == 409
    assert started.status_code == 200


@pytest.mark.asyncio
async def test_two_api_cycles_preserve_last_active_version_when_next_dream_fails(
    tmp_path: Path,
) -> None:
    class FailingWritebackBackend(DeterministicWritebackBackend):
        def render_user_persona(self, user_profile: str, limit: int) -> str:
            raise RuntimeError("simulated provider outage")

    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_VALIDATION_REQUIRE_ACTIVE_WRITEBACK=true\n",
        encoding="utf-8",
    )
    app = create_app(tmp_path, env_file=env_file)
    transport = httpx.ASGITransport(app=app)
    scope = {
        "tenant_id": "dream-lab",
        "agent_id": "enterprise-colleague",
        "user_id": "project-manager",
    }

    async def import_event(client: httpx.AsyncClient, number: int) -> None:
        assistant = "Always verify before risky action."
        response = await client.post(
            "/v1/validation/import",
            content=_manual_line(
                event_id=f"evt-cycle-{number}",
                session_id=f"session-{number}",
                task_id=f"task-{number}",
                messages=[
                    {"role": "user", "content": "I prefer concise answers"},
                    {"role": "assistant", "content": assistant},
                ],
                final_response=assistant,
            )
            + "\n",
            headers={"Content-Type": "application/x-ndjson"},
        )
        assert response.status_code == 200

    async def dream_latest(client: httpx.AsyncClient) -> dict[str, object]:
        candidate = await client.post("/v1/validation/dream", json=scope)
        assert candidate.status_code == 200, candidate.text
        assert candidate.json()["status"] == "active"
        return candidate.json()

    async with httpx.AsyncClient(
        transport=transport, base_url="http://dream.test"
    ) as client:
        await import_event(client, 1)
        await dream_latest(client)
        await import_event(client, 2)
        second = await dream_latest(client)
        stable_context = app.state.dream_service.start_context(
            app.state.dream_service.ledger.read_all()[-1].scope
        )
        app.state.closed_loop.writeback_backend = FailingWritebackBackend()
        await import_event(client, 3)
        failed = await client.post("/v1/validation/dream", json=scope)
        status_response = await client.get(
            "/v1/validation/publications/status",
            params=scope,
        )

    status_payload = status_response.json()
    restored_context = app.state.dream_service.start_context(
        app.state.dream_service.ledger.read_all()[-1].scope
    )
    assert second["version"] == 2
    assert failed.status_code == 503
    assert status_payload["active"]["version"] == 2
    assert status_payload["latest"]["status"] == "failed"
    assert restored_context["user_profile"] == stable_context["user_profile"]
    assert restored_context["decision_rules"] == stable_context["decision_rules"]
