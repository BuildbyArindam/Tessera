"""The LLM boundary: one protocol, one real provider, one scripted fake.

Everything else in the agent package talks to `LLMClient` and nothing else, so the planner never
knows which provider is behind it and unit tests never touch a network. The model's reply is either
text or tool calls; it can never do anything else, because this module has no way to act.

`AnthropicLLM` is the only real provider (the simplest choice; see docs/daily-log.md, Day 9). It
imports the SDK lazily, reads its key from the environment, never logs it, and turns every SDK failure
into `LLMError` carrying only the exception class name, so a secret in an SDK message cannot reach a log.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_MAX_TOKENS = 4096


class LLMError(RuntimeError):
    """The model could not be reached or returned something unusable. The planner fails safe on it."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    content: str  # a JSON string


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()  # assistant turns only
    tool_results: tuple[ToolResult, ...] = ()  # user turns only


@dataclass(frozen=True)
class LLMResponse:
    """Text, tool calls, or both. `model` is the model that answered, recorded with every run."""

    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    model: str = ""


class LLMClient(Protocol):
    @property
    def model(self) -> str: ...

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        temperature: float = 0.0,
    ) -> LLMResponse: ...


# ---- scripted fake for tests ---------------------------------------------------------------------
@dataclass(frozen=True)
class FakeCall:
    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    temperature: float


@dataclass
class FakeLLM:
    """Returns scripted replies in order and records every call it receives.

    A script entry is a string (a text reply), an LLMResponse, or an Exception to raise. Running out of
    script is an LLMError, so a test that makes an unexpected extra call fails loudly.
    """

    script: Sequence[str | LLMResponse | Exception]
    model: str = "fake-llm"
    calls: list[FakeCall] = field(default_factory=list)

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls.append(FakeCall(system, tuple(messages), tuple(tools), temperature))
        index = len(self.calls) - 1
        if index >= len(self.script):
            raise LLMError("FakeLLM script exhausted")
        item = self.script[index]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return LLMResponse(text=item, model=self.model)
        return LLMResponse(text=item.text, tool_calls=item.tool_calls, model=item.model or self.model)


def tool_call_reply(name: str, arguments: Mapping[str, Any], call_id: str = "call-1") -> LLMResponse:
    """Scripted reply that asks for one tool call."""
    return LLMResponse(tool_calls=(ToolCall(id=call_id, name=name, arguments=dict(arguments)),))


# ---- Anthropic Messages API ----------------------------------------------------------------------
def _to_wire(messages: Sequence[Message]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    for message in messages:
        blocks: list[dict[str, Any]] = []
        if message.text:
            blocks.append({"type": "text", "text": message.text})
        blocks += [
            {"type": "tool_use", "id": c.id, "name": c.name, "input": dict(c.arguments)} for c in message.tool_calls
        ]
        blocks += [
            {"type": "tool_result", "tool_use_id": r.tool_call_id, "content": r.content} for r in message.tool_results
        ]
        if not blocks:
            raise LLMError("a message must carry text, tool calls or tool results")
        wire.append({"role": message.role, "content": blocks})
    return wire


class AnthropicLLM:
    """Messages API client. Key from `LLM_API_KEY`, model from `LLM_MODEL` (default `DEFAULT_MODEL`)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = 60.0,
        client: Any = None,
    ) -> None:
        self._model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        self._max_tokens = max_tokens
        if client is not None:  # injected (tests): no key and no SDK needed
            self._client = client
            return
        key = api_key or os.environ.get("LLM_API_KEY")
        if not key:
            raise LLMError("LLM_API_KEY is not set")
        try:
            import anthropic  # lazy: the SDK is an optional extra
        except ImportError:
            raise LLMError("the 'anthropic' package is not installed (pip install tessera-agent[anthropic])") from None
        self._client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=2)

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        temperature: float = 0.0,
    ) -> LLMResponse:
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": _to_wire(messages),
            "temperature": temperature,
        }
        if tools:
            request["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": dict(t.input_schema)} for t in tools
            ]
        try:
            response = self._client.messages.create(**request)
        except Exception as exc:  # provider boundary: keep only the class name, never the message
            raise LLMError(f"provider call failed: {type(exc).__name__}") from None
        return self._parse(response)

    def _parse(self, response: Any) -> LLMResponse:
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in getattr(response, "content", None) or []:
            kind = getattr(block, "type", None)
            if kind == "text":
                texts.append(str(block.text))
            elif kind == "tool_use":
                arguments = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=str(block.id), name=str(block.name), arguments=arguments))
        return LLMResponse(text="".join(texts) or None, tool_calls=tuple(calls), model=self._model)


def llm_from_env(env: Mapping[str, str] | None = None) -> LLMClient:
    """Build the provider named by `LLM_PROVIDER` (default `anthropic`)."""
    source = os.environ if env is None else env
    provider = (source.get("LLM_PROVIDER") or "anthropic").strip().lower()
    if provider == "anthropic":
        return AnthropicLLM(api_key=source.get("LLM_API_KEY"), model=source.get("LLM_MODEL"))
    raise LLMError(f"unsupported LLM_PROVIDER {provider!r}; only 'anthropic' is implemented")
