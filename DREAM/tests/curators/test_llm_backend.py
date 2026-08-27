import json
from types import SimpleNamespace

from dream.curators.backend import OpenAICuratorBackend


class RoutingCompletions:
    def create(self, **kwargs: object) -> object:
        tool_name = kwargs["tools"][0]["function"]["name"]
        if tool_name == "curate_ai_decisions":
            arguments = {
                "decision_rules_markdown": (
                    "# AI Decision Rules\n\n- 高风险操作先验证。\n"
                    "  - Evidence cards: verify-risk-a, verify-risk-b\n"
                ),
                "archive_card_ids": ["verify-risk-b"],
                "summary": "Merged two overlapping decision cards.",
            }
        else:
            arguments = {
                "user_profile_markdown": (
                    "Prefers concise answers.\n"
                    "<!-- dream-sources: evt-1, evt-2 -->\n"
                ),
                "summary": "Merged duplicate preference evidence.",
            }
        call = SimpleNamespace(
            function=SimpleNamespace(
                name=tool_name, arguments=json.dumps(arguments)
            )
        )
        message = SimpleNamespace(tool_calls=[call], content=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_llm_curator_backend_returns_ai_and_user_semantic_plans() -> None:
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=RoutingCompletions())
    )
    backend = OpenAICuratorBackend(client=client, model="curator-model")

    ai = backend.curate_ai(
        cards=(
            ("verify-risk-a", "# A\n\n## 决策原则\n\n高风险操作先验证。"),
            ("verify-risk-b", "# B\n\n## 决策原则\n\n不可逆操作应先确认。"),
        ),
        current_rules="",
    )
    user = backend.curate_user(
        "Prefers concise answers.\n<!-- dream-source: evt-1 -->\n"
        "§\nPrefers brief replies.\n<!-- dream-source: evt-2 -->\n"
    )

    assert ai.archive_card_ids == ("verify-risk-b",)
    assert "Evidence cards" in ai.decision_rules_markdown
    assert "evt-1" in user.user_profile_markdown
    assert "evt-2" in user.user_profile_markdown


def test_llm_curator_accepts_json_only_provider() -> None:
    class JsonOnlyCompletions:
        def create(self, **kwargs: object) -> object:
            if "tools" in kwargs:
                raise RuntimeError("tools unsupported")
            content = json.dumps(
                {
                    "tool_calls": [
                        {
                            "name": "curate_user_profile",
                            "arguments": {
                                "user_profile_markdown": (
                                    "Prefers concise answers.\n"
                                    "<!-- dream-sources: evt-1 -->\n"
                                ),
                                "summary": "Kept one preference.",
                            },
                        }
                    ]
                }
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    backend = OpenAICuratorBackend(
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=JsonOnlyCompletions())
        ),
        model="agnes-model",
    )

    result = backend.curate_user(
        "Prefers concise answers.\n<!-- dream-source: evt-1 -->\n"
    )

    assert result.summary == "Kept one preference."
    assert "evt-1" in result.user_profile_markdown
