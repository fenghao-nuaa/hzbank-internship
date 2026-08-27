import json
from types import SimpleNamespace

import pytest

from dream.extraction.structured import (
    StructuredCompletionClient,
    StructuredCompletionError,
)


SUMMARY_TOOL = {
    "type": "function",
    "function": {
        "name": "summarize",
        "parameters": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
}


class RejectToolsThenReturnJson:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs: object) -> object:
        self.calls += 1
        if "tools" in kwargs:
            raise RuntimeError("tools unsupported")
        content = json.dumps(
            {"tool_calls": [{"name": "summarize", "arguments": {"summary": "ok"}}]}
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class AlwaysFail:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs: object) -> object:
        self.calls += 1
        raise RuntimeError("Authorization: Bearer secret")


def client_for(completions: object) -> object:
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def test_auto_mode_falls_back_to_json_when_tools_are_unsupported() -> None:
    completions = RejectToolsThenReturnJson()

    result = StructuredCompletionClient(client_for(completions), "agnes", 3).call(
        system="Return the schema.",
        content="input",
        tools=(SUMMARY_TOOL,),
        forced_tool="summarize",
        mode="auto",
    )

    assert result[0].arguments == {"summary": "ok"}
    assert completions.calls == 2


def test_structured_call_stops_after_initial_attempt_plus_two_retries() -> None:
    completions = AlwaysFail()

    with pytest.raises(StructuredCompletionError) as error:
        StructuredCompletionClient(client_for(completions), "agnes", 3).call(
            system="system",
            content="input",
            tools=(SUMMARY_TOOL,),
            forced_tool="summarize",
            mode="tools",
        )

    assert completions.calls == 3
    assert "secret" not in str(error.value)


def test_json_mode_rejects_unknown_tool_name() -> None:
    class UnknownTool:
        def create(self, **kwargs: object) -> object:
            content = '{"tool_calls":[{"name":"unknown","arguments":{}}]}'
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    with pytest.raises(StructuredCompletionError):
        StructuredCompletionClient(client_for(UnknownTool()), "agnes", 3).call(
            system="system",
            content="input",
            tools=(SUMMARY_TOOL,),
            forced_tool=None,
            mode="json",
        )


def test_json_mode_accepts_one_markdown_json_code_fence() -> None:
    class FencedJson:
        def create(self, **kwargs: object) -> object:
            content = (
                "```json\n"
                '{"tool_calls":[{"name":"summarize",'
                '"arguments":{"summary":"ok"}}]}\n'
                "```"
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    result = StructuredCompletionClient(
        client_for(FencedJson()), "agnes", 3
    ).call_once(
        system="system",
        content="input",
        tools=(SUMMARY_TOOL,),
        forced_tool="summarize",
        mode="json",
    )

    assert result[0].arguments == {"summary": "ok"}


@pytest.mark.parametrize(
    "content",
    [
        'Result:\n{"summary":"ok"}\nEnd of result.',
        'Result:\n```json\n{"summary":"ok"}\n```\nEnd of result.',
    ],
)
def test_json_mode_accepts_one_json_result_surrounded_by_prose(
    content: str,
) -> None:
    class ProseWrappedJson:
        def create(self, **kwargs: object) -> object:
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    result = StructuredCompletionClient(
        client_for(ProseWrappedJson()), "agnes", 3
    ).call_once(
        system="system",
        content="input",
        tools=(SUMMARY_TOOL,),
        forced_tool="summarize",
        mode="json",
    )

    assert result[0].arguments == {"summary": "ok"}


def test_json_mode_rejects_multiple_prose_wrapped_json_results() -> None:
    class MultipleJsonResults:
        def create(self, **kwargs: object) -> object:
            content = '{"summary":"first"}\n{"summary":"second"}'
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    with pytest.raises(json.JSONDecodeError):
        StructuredCompletionClient(
            client_for(MultipleJsonResults()), "agnes", 3
        ).call_once(
            system="system",
            content="input",
            tools=(SUMMARY_TOOL,),
            forced_tool="summarize",
            mode="json",
        )


def test_json_mode_reports_truncated_completion_before_decoding() -> None:
    class TruncatedJson:
        def create(self, **kwargs: object) -> object:
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="length",
                        message=SimpleNamespace(content='{"summary":"incomplete'),
                    )
                ]
            )

    with pytest.raises(ValueError, match="structured output truncated"):
        StructuredCompletionClient(
            client_for(TruncatedJson()), "agnes", 3
        ).call_once(
            system="system",
            content="input",
            tools=(SUMMARY_TOOL,),
            forced_tool="summarize",
            mode="json",
        )


def test_json_repair_prompt_includes_forced_tool_schema() -> None:
    class RecordingJson:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def create(self, **kwargs: object) -> object:
            self.kwargs = kwargs
            content = json.dumps(
                {
                    "tool_calls": [
                        {"name": "summarize", "arguments": {"summary": "ok"}}
                    ]
                }
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    completions = RecordingJson()
    StructuredCompletionClient(client_for(completions), "agnes", 3).call_once(
        system="system",
        content="input",
        tools=(SUMMARY_TOOL,),
        forced_tool="summarize",
        mode="json",
    )

    system_prompt = str(completions.kwargs["messages"][0]["content"])
    assert '"name":"summarize"' in system_prompt
    assert '"summary"' in system_prompt


def test_json_mode_accepts_direct_forced_tool_arguments() -> None:
    class DirectArguments:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def create(self, **kwargs: object) -> object:
            self.kwargs = kwargs
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"summary":"ok"}')
                    )
                ]
            )

    completions = DirectArguments()
    result = StructuredCompletionClient(
        client_for(completions), "agnes", 3
    ).call_once(
        system="system",
        content="input",
        tools=(SUMMARY_TOOL,),
        forced_tool="summarize",
        mode="json",
    )

    assert result[0].name == "summarize"
    assert result[0].arguments == {"summary": "ok"}
    system_prompt = str(completions.kwargs["messages"][0]["content"])
    assert "direct arguments object" in system_prompt
