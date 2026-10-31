"""tessera_agent: the LLM planner. It proposes actions and can never execute them.

May read `tessera_core` types. May NOT import `tessera_airwallex` or any database or HTTP-server code
(tests/unit/test_agent_isolation.py enforces this now; import-linter takes over on Day 13).
"""

from tessera_agent.context import Obligation, PlannerSnapshot, build_context
from tessera_agent.llm import (
    AnthropicLLM,
    FakeLLM,
    LLMClient,
    LLMError,
    LLMResponse,
    Message,
    ToolCall,
    ToolResult,
    ToolSpec,
    llm_from_env,
    tool_call_reply,
)
from tessera_agent.planner import Planner, PlanRecord, PlanResult
from tessera_agent.prompts import PromptFile, load_prompt
from tessera_agent.schemas import MAX_PROPOSALS, ProposalList
from tessera_agent.tools import ReadOnlyTools

__all__ = [
    "MAX_PROPOSALS",
    "AnthropicLLM",
    "FakeLLM",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "Message",
    "Obligation",
    "PlanRecord",
    "PlanResult",
    "Planner",
    "PlannerSnapshot",
    "PromptFile",
    "ProposalList",
    "ReadOnlyTools",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "build_context",
    "llm_from_env",
    "load_prompt",
    "tool_call_reply",
]
