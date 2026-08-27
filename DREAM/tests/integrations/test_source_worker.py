import asyncio
from pathlib import Path

import httpx
import pytest

from dream.api import create_app
from tests.integrations.source_helpers import source_line, write_source_env


@pytest.mark.asyncio
async def test_worker_pulls_memory_into_ai_card_and_isolated_user_profile(
    tmp_path: Path,
) -> None:
    env_file = write_source_env(tmp_path, interval_seconds=1)
    calls: list[str] = []

    def source_handler(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after", "")
        calls.append(after)
        if after:
            return httpx.Response(200, text="")
        return httpx.Response(
            200,
            text=source_line(
                user_id="alice",
                messages=[
                    {"role": "user", "content": "I prefer concise answers"},
                    {
                        "role": "assistant",
                        "content": "Always verify before risky action",
                    },
                ],
                final_response="Verified before applying the change.",
            )
            + "\n",
        )

    app = create_app(
        tmp_path,
        worker_interval_seconds=0.01,
        env_file=env_file,
        source_transport=httpx.MockTransport(source_handler),
    )
    alice = {"tenant_id": "acme", "agent_id": "assistant", "user_id": "alice"}
    bob = {"tenant_id": "acme", "agent_id": "assistant", "user_id": "bob"}

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://dream.test"
        ) as client:
            deadline = asyncio.get_running_loop().time() + 1.0
            alice_context: dict[str, object] = {}
            while asyncio.get_running_loop().time() < deadline:
                alice_context = (
                    await client.post("/v1/tasks/start", json=alice)
                ).json()
                if (
                    "Prefers concise answers" in alice_context["user_profile"]
                    and alice_context["decision_cards"]
                ):
                    break
                await asyncio.sleep(0.01)

            bob_context = (
                await client.post("/v1/tasks/start", json=bob)
            ).json()

    assert calls == [""]
    assert "Prefers concise answers" in alice_context["user_profile"]
    assert any(
        "高风险操作前先验证" in card
        for card in alice_context["decision_cards"]
    )
    assert "Prefers concise answers" not in bob_context["user_profile"]
    assert bob_context["decision_cards"] == alice_context["decision_cards"]
