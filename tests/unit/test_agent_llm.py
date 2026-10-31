"""Day 9: the LLM boundary. No network: the provider client is exercised through an injected fake SDK."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tessera_agent import (
    AnthropicLLM,
    FakeLLM,
    LLMError,
    LLMResponse,
    Message,
    ToolCall,
    ToolResult,
    ToolSpec,
    llm_from_env,
    tool_call_reply,
)
from tessera_agent.llm import DEFAULT_MODEL

pytestmark = pytest.mark.usefixtures("no_network")

SPEC = ToolSpec(name="get_forecast", description="d", input_schema={"type": "object", "properties": {}})


def ask(llm: Any, *, tools: tuple[ToolSpec, ...] = ()) -> LLMResponse:
    return llm.complete(system="sys", messages=[Message("user", text="hi")], tools=tools, temperature=0.0)


# ---- FakeLLM -----------------------------------------------------------------------------------
def test_fake_returns_scripted_replies_in_order_and_records_calls() -> None:
    fake = FakeLLM(["one", tool_call_reply("get_forecast", {"currency": "USD"}), LLMResponse(text="three")])
    assert ask(fake).text == "one"
    second = ask(fake, tools=(SPEC,))
    assert second.tool_calls[0].name == "get_forecast" and second.model == "fake-llm"
    assert ask(fake).text == "three"
    assert [c.temperature for c in fake.calls] == [0.0, 0.0, 0.0]
    assert fake.calls[1].tools == (SPEC,) and fake.calls[0].system == "sys"


def test_fake_raises_scripted_errors_and_fails_loudly_when_the_script_runs_out() -> None:
    fake = FakeLLM([LLMError("down")])
    with pytest.raises(LLMError, match="down"):
        ask(fake)
    with pytest.raises(LLMError, match="exhausted"):
        ask(fake)


# ---- AnthropicLLM through a fake SDK ----------------------------------------------------------
class FakeSdk:
    def __init__(self, content: list[Any] | None = None, error: Exception | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self._content, self._error = content or [], error
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(content=self._content)


def test_request_shape_has_system_temperature_tools_and_tool_round_trip() -> None:
    sdk = FakeSdk([SimpleNamespace(type="text", text="done")])
    llm = AnthropicLLM(client=sdk, model="m-1")
    history = [
        Message("user", text="task"),
        Message("assistant", text="calling", tool_calls=(ToolCall("t1", "get_forecast", {"currency": "USD"}),)),
        Message("user", tool_results=(ToolResult("t1", '{"ok":true}'),)),
    ]
    out = llm.complete(system="SYS", messages=history, tools=(SPEC,), temperature=0.0)
    req = sdk.requests[0]
    assert out.text == "done" and out.model == "m-1" and llm.model == "m-1"
    assert req["model"] == "m-1" and req["system"] == "SYS" and req["temperature"] == 0.0
    assert req["tools"] == [{"name": "get_forecast", "description": "d", "input_schema": dict(SPEC.input_schema)}]
    assistant, results = req["messages"][1], req["messages"][2]
    assert assistant["content"][1] == {
        "type": "tool_use",
        "id": "t1",
        "name": "get_forecast",
        "input": {"currency": "USD"},
    }
    assert results["content"] == [{"type": "tool_result", "tool_use_id": "t1", "content": '{"ok":true}'}]


def test_tools_key_is_omitted_when_no_tools_are_offered() -> None:
    sdk = FakeSdk([SimpleNamespace(type="text", text="x")])
    ask(AnthropicLLM(client=sdk))
    assert "tools" not in sdk.requests[0]


def test_tool_use_blocks_become_tool_calls() -> None:
    blocks = [
        SimpleNamespace(type="text", text="thinking"),
        SimpleNamespace(type="tool_use", id="a", name="get_fx_range", input={"pair": "USD/INR", "horizon_days": 7}),
        SimpleNamespace(type="tool_use", id="b", name="x", input="not-a-dict"),
    ]
    out = ask(AnthropicLLM(client=FakeSdk(blocks)))
    assert out.text == "thinking"
    assert out.tool_calls[0] == ToolCall("a", "get_fx_range", {"pair": "USD/INR", "horizon_days": 7})
    assert out.tool_calls[1].arguments == {}  # a non-object input never reaches a tool as-is


def test_sdk_errors_become_llm_error_without_leaking_the_message() -> None:
    secret = "sk-live-SECRET-123"
    llm = AnthropicLLM(client=FakeSdk(error=RuntimeError(f"401 bad key {secret}")))
    with pytest.raises(LLMError) as info:
        ask(llm)
    assert "RuntimeError" in str(info.value) and secret not in str(info.value)
    assert info.value.__cause__ is None  # `from None`: the original message is not chained either


def test_empty_messages_are_refused_before_any_call() -> None:
    sdk = FakeSdk()
    with pytest.raises(LLMError):
        AnthropicLLM(client=sdk).complete(system="s", messages=[Message("user")], tools=(), temperature=0.0)
    assert sdk.requests == []


# ---- configuration from the environment ---------------------------------------------------------
def test_missing_key_is_a_clear_error_and_the_key_is_read_from_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(LLMError, match="LLM_API_KEY"):
        AnthropicLLM()
    with pytest.raises(LLMError, match="LLM_API_KEY"):
        llm_from_env({"LLM_PROVIDER": "anthropic"})


def test_unsupported_provider_is_refused() -> None:
    with pytest.raises(LLMError, match="unsupported"):
        llm_from_env({"LLM_PROVIDER": "somebody-else", "LLM_API_KEY": "x"})


def test_default_model_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert AnthropicLLM(client=FakeSdk()).model == DEFAULT_MODEL
    monkeypatch.setenv("LLM_MODEL", "other-model")
    assert AnthropicLLM(client=FakeSdk()).model == "other-model"
