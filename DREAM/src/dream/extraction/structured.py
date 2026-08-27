"""Provider-neutral structured chat completions with safe bounded retries."""

from dataclasses import dataclass
import json
import re
from typing import Any


@dataclass(frozen=True)
class StructuredToolCall:
    name: str
    arguments: dict[str, object]


class StructuredCompletionError(RuntimeError):
    """Safe failure that excludes provider responses and credentials."""


class StructuredProviderError(RuntimeError):
    """A provider call failed before structured output could be inspected."""


def _attribute(value: object, name: str, default: object = None) -> object:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _decode_json_payload(value: object) -> object:
    text = str(value).strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) < 3 or lines[0].strip().casefold() not in {"```", "```json"}:
            raise json.JSONDecodeError("invalid JSON code fence", text, 0)
        text = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as original_error:
        fenced = re.findall(
            r"```(?:json)?\s*(.*?)\s*```",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if len(fenced) == 1:
            return json.loads(fenced[0].strip())
        if fenced:
            raise original_error

        starts = [index for token in ("{", "[") if (index := text.find(token)) >= 0]
        if not starts:
            raise original_error
        start = min(starts)
        payload, consumed = json.JSONDecoder().raw_decode(text[start:])
        suffix = text[start + consumed :]
        if "{" in suffix or "[" in suffix:
            raise original_error
        return payload


class StructuredCompletionClient:
    def __init__(
        self,
        client: Any,
        model: str,
        max_attempts: int = 3,
        max_completion_tokens: int | None = None,
    ) -> None:
        if max_attempts != 3:
            raise ValueError("structured completion uses exactly three total attempts")
        self.client = client
        self.model = model
        self.max_attempts = max_attempts
        self.max_completion_tokens = max_completion_tokens

    def call(
        self,
        *,
        system: str,
        content: str,
        tools: tuple[dict[str, object], ...],
        forced_tool: str | None,
        mode: str,
        allow_empty: bool = False,
    ) -> tuple[StructuredToolCall, ...]:
        if mode not in {"auto", "tools", "json"}:
            raise ValueError("structured mode must be auto, tools, or json")
        if not tools:
            return ()
        attempts = (
            ("tools", "json", "json") if mode == "auto" else (mode,) * self.max_attempts
        )
        last_category = "structured completion failed"
        for selected in attempts:
            try:
                return self._call_once(
                    system=system,
                    content=content,
                    tools=tools,
                    forced_tool=forced_tool,
                    mode=selected,
                    allow_empty=allow_empty,
                )
            except Exception as exc:
                last_category = type(exc).__name__
        raise StructuredCompletionError(last_category)

    def call_once(
        self,
        *,
        system: str,
        content: str,
        tools: tuple[dict[str, object], ...],
        forced_tool: str | None,
        mode: str,
        allow_empty: bool = False,
    ) -> tuple[StructuredToolCall, ...]:
        """Make exactly one provider request without implicit retries or fallback."""

        selected_mode = "tools" if mode == "auto" else mode
        if selected_mode not in {"tools", "json"}:
            raise ValueError("structured mode must be auto, tools, or json")
        if not tools:
            return ()
        return self._call_once(
            system=system,
            content=content,
            tools=tools,
            forced_tool=forced_tool,
            mode=selected_mode,
            allow_empty=allow_empty,
        )

    def _call_once(
        self,
        *,
        system: str,
        content: str,
        tools: tuple[dict[str, object], ...],
        forced_tool: str | None,
        mode: str,
        allow_empty: bool,
    ) -> tuple[StructuredToolCall, ...]:
        allowed = {str(tool["function"]["name"]) for tool in tools}
        kwargs: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": 0,
        }
        if self.max_completion_tokens is not None:
            kwargs["max_completion_tokens"] = self.max_completion_tokens
        if mode == "tools":
            kwargs["tools"] = list(tools)
            kwargs["tool_choice"] = (
                {"type": "function", "function": {"name": forced_tool}}
                if forced_tool
                else ("auto" if allow_empty else "required")
            )
        else:
            kwargs["response_format"] = {"type": "json_object"}
            tool_specs = [
                {
                    "name": str(tool["function"]["name"]),
                    "parameters": tool["function"]["parameters"],
                }
                for tool in tools
            ]
            schema_text = json.dumps(tool_specs, separators=(",", ":"))
            if forced_tool is not None:
                kwargs["messages"][0]["content"] = (
                    f"{system}\nReturn only the direct arguments object for the "
                    f"forced tool {json.dumps(forced_tool)} as JSON. "
                    "The arguments must follow this tool schema exactly: "
                    f"{schema_text}"
                )
            else:
                envelope = {
                    "tool_calls": [
                        {"name": "one allowed tool", "arguments": {}}
                    ]
                }
                kwargs["messages"][0]["content"] = (
                    f"{system}\nReturn only JSON with this envelope: "
                    f"{json.dumps(envelope, separators=(',', ':'))}\n"
                    "The arguments must follow one of these tool schemas exactly: "
                    f"{schema_text}"
                )
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            category = type(exc).__name__
            status_code = getattr(exc, "status_code", None)
            if (
                isinstance(status_code, int)
                and not isinstance(status_code, bool)
                and 100 <= status_code <= 599
            ):
                category = f"{category} HTTP {status_code}"
            raise StructuredProviderError(category) from exc
        choices = _attribute(response, "choices", []) or []
        if not choices:
            raise ValueError("no choices")
        if _attribute(choices[0], "finish_reason", "") == "length":
            raise ValueError("structured output truncated")
        message = _attribute(choices[0], "message")
        if mode == "tools":
            raw_calls = _attribute(message, "tool_calls", []) or []
            calls = [
                {
                    "name": str(_attribute(_attribute(call, "function"), "name", "")),
                    "arguments": json.loads(
                        str(_attribute(_attribute(call, "function"), "arguments", "{}"))
                    ),
                }
                for call in raw_calls
            ]
        else:
            payload = _decode_json_payload(_attribute(message, "content", ""))
            if isinstance(payload, dict) and "tool_calls" in payload:
                calls = payload.get("tool_calls", [])
            elif isinstance(payload, dict) and forced_tool is not None:
                calls = [{"name": forced_tool, "arguments": payload}]
            else:
                calls = []
        result: list[StructuredToolCall] = []
        for call in calls:
            name = str(call.get("name", ""))
            arguments = call.get("arguments")
            if name not in allowed or not isinstance(arguments, dict):
                raise ValueError("invalid structured tool call")
            if forced_tool is not None and name != forced_tool:
                raise ValueError("unexpected forced tool")
            result.append(StructuredToolCall(name=name, arguments=arguments))
        if not result and not allow_empty:
            raise ValueError("no structured tool calls")
        return tuple(result)
