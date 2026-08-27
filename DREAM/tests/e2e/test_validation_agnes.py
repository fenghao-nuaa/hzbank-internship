from pathlib import Path
from types import SimpleNamespace

import pytest

from dream.validation.agnes import AgnesSimulator, AgnesSimulatorError


class FakeCompletions:
    def __init__(self, outputs: list[str], *, tool_calls: object = None) -> None:
        self.outputs = outputs
        self.tool_calls = tool_calls
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        content = self.outputs[len(self.requests) - 1]
        message = SimpleNamespace(content=content, tool_calls=self.tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def simulator(outputs: list[str], *, tool_calls: object = None):
    completions = FakeCompletions(outputs, tool_calls=tool_calls)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return AgnesSimulator(client, "agnes-model"), completions


def test_simulator_returns_only_user_message_and_persists_nothing(
    tmp_path: Path,
) -> None:
    agnes, completions = simulator(["请先给出结论。"])

    message = agnes.next_user_message(
        hidden_persona={
            "role": "project manager",
            "preference": "conclusions first",
        },
        public_history=(),
        task_number=1,
    )

    assert message == "请先给出结论。"
    assert completions.requests[0]["model"] == "agnes-model"
    assert list(tmp_path.rglob("*")) == []


def test_each_simulation_request_contains_only_its_current_hidden_persona() -> None:
    agnes, completions = simulator(["先给结论。", "请给我一个例子。"])

    agnes.next_user_message(
        hidden_persona={"role": "manager-secret"},
        public_history=(),
        task_number=1,
    )
    agnes.next_user_message(
        hidden_persona={"role": "beginner-secret"},
        public_history=({"role": "assistant", "content": "你好"},),
        task_number=2,
    )

    first = str(completions.requests[0]["messages"])
    second = str(completions.requests[1]["messages"])
    assert "manager-secret" in first
    assert "manager-secret" not in second
    assert "beginner-secret" in second


@pytest.mark.parametrize(
    "output,tool_calls",
    [
        ("", None),
        ('{"message":"先给结论"}', None),
        ("Assistant: 请先给结论。", None),
        ("请先给结论。", [object()]),
    ],
)
def test_simulator_rejects_non_user_message_outputs(
    output: str, tool_calls: object
) -> None:
    agnes, _ = simulator([output], tool_calls=tool_calls)

    with pytest.raises(AgnesSimulatorError):
        agnes.next_user_message(
            hidden_persona={"role": "manager"},
            public_history=(),
            task_number=1,
        )


def test_public_history_rejects_dream_artifact_fields() -> None:
    agnes, _ = simulator(["下一条消息"])

    with pytest.raises(AgnesSimulatorError, match="public history"):
        agnes.next_user_message(
            hidden_persona={"role": "manager"},
            public_history=(
                {
                    "role": "assistant",
                    "content": "公开回复",
                    "user_profile": "DREAM private artifact",
                },
            ),
            task_number=1,
        )
