"""In-memory-only Agnes client used to generate synthetic user messages."""

import json
import re
from typing import Any, Mapping, Sequence


_ASSISTANT_PREFIX = re.compile(r"^(?:assistant|ai|助手)\s*[:：]", re.IGNORECASE)


class AgnesSimulatorError(ValueError):
    """Agnes returned output that cannot be used as one user message."""


class AgnesSimulator:
    def __init__(self, client: Any, model: str) -> None:
        if not model.strip():
            raise ValueError("Agnes model must not be blank")
        self.client = client
        self.model = model

    def _public_messages(
        self, public_history: Sequence[Mapping[str, object]]
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for item in public_history:
            if set(item) != {"role", "content"}:
                raise AgnesSimulatorError(
                    "public history may contain only role and content"
                )
            role = item["role"]
            content = item["content"]
            if role not in {"user", "assistant"} or not isinstance(content, str):
                raise AgnesSimulatorError("public history contains an invalid message")
            if not content.strip():
                raise AgnesSimulatorError("public history contains a blank message")
            messages.append({"role": str(role), "content": content})
        return messages

    def next_user_message(
        self,
        *,
        hidden_persona: Mapping[str, object],
        public_history: Sequence[Mapping[str, object]],
        task_number: int,
    ) -> str:
        if task_number < 1:
            raise AgnesSimulatorError("task number must be positive")
        if not hidden_persona:
            raise AgnesSimulatorError("hidden persona must not be empty")
        try:
            persona = json.dumps(
                dict(hidden_persona),
                ensure_ascii=False,
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise AgnesSimulatorError("hidden persona must be JSON-compatible") from exc

        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "Act only as the synthetic user described below. Generate exactly "
                    "one natural next user message for the public conversation. Do not "
                    "mention the persona, do not answer as the assistant, do not use "
                    "tools, and do not return JSON.\n"
                    f"Task number: {task_number}\nHidden persona: {persona}"
                ),
            },
            *self._public_messages(public_history),
            {
                "role": "user",
                "content": "Return only the next synthetic user message.",
            },
        ]
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
            )
            result = response.choices[0].message
        except Exception as exc:
            raise AgnesSimulatorError("Agnes simulation request failed") from exc
        if getattr(result, "tool_calls", None):
            raise AgnesSimulatorError("Agnes simulator must not call tools")
        content = getattr(result, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise AgnesSimulatorError("Agnes simulator returned a blank message")
        rendered = content.strip()
        if _ASSISTANT_PREFIX.match(rendered):
            raise AgnesSimulatorError("Agnes simulator answered as the assistant")
        try:
            decoded = json.loads(rendered)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            raise AgnesSimulatorError("Agnes simulator returned a JSON object")
        return rendered
