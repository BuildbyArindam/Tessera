"""The planner: context in, validated proposals out. It decides nothing and executes nothing.

One run:
  1. build the context JSON from the snapshot and hash it;
  2. call the model at temperature 0 with the read-only tools, answering any tool calls (bounded);
  3. validate the final text (validate.py). On failure, retry ONCE with a field-path-only error list;
  4. still invalid, or the provider failed: return an EMPTY list. Never a partial list, never a crash.

Every run returns a `PlanRecord` with the model name, prompt hash, context hash, every tool call
(arguments and a hash of the result), the evidence ids issued, and any validation errors, which is what
the Day 11 audit log will store. Proposals get deterministic ids derived from the context and the
proposal itself, so planning the same state twice yields the same ids and cannot double-queue an action.

The planner returns `Proposal` objects only. They carry no authority: they go to the policy engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import JsonValue
from tessera_core.domain import ActionType, Proposal
from tessera_core.domain.base import DomainModel

from tessera_agent.canon import canonical_json, sha256_hex
from tessera_agent.context import PlannerSnapshot, build_context
from tessera_agent.llm import LLMClient, LLMError, LLMResponse, Message, ToolResult
from tessera_agent.prompts import PromptFile, load_prompt
from tessera_agent.schemas import ProposedAction
from tessera_agent.tools import ReadOnlyTools
from tessera_agent.validate import validate_reply

TEMPERATURE = 0.0
MAX_TOOL_ROUNDS = 6
MAX_TOOL_CALLS = 12
MAX_RAW_CHARS = 20_000

Outcome = Literal["ok", "ok_empty", "invalid_output", "llm_error"]


class ToolTrace(DomainModel):
    name: str
    arguments_json: str
    evidence_id: str | None  # None when the tool returned an error
    result_sha256: str


class PlanRecord(DomainModel):
    model: str
    prompt_name: str
    prompt_hash: str
    context_hash: str
    temperature: float
    outcome: Outcome
    attempts: int  # validation attempts made (1 or 2)
    llm_calls: int
    tool_calls: tuple[ToolTrace, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()  # of the LAST failed attempt; empty when valid
    raw_output: str | None = None  # the model's final text, truncated


class PlanResult(DomainModel):
    proposals: tuple[Proposal, ...]
    evidence: dict[str, tuple[str, ...]]  # proposal id -> the evidence ids it cites
    record: PlanRecord


@dataclass
class _Run:
    """Mutable bookkeeping for one run. Never leaves this module."""

    messages: list[Message]
    tools: ReadOnlyTools
    llm_calls: int = 0
    traces: list[ToolTrace] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


class Planner:
    def __init__(self, llm: LLMClient, prompt: PromptFile | None = None) -> None:
        self._llm = llm
        self._prompt = prompt or load_prompt()

    def run(self, snapshot: PlannerSnapshot, *, now: datetime) -> PlanResult:
        context_json = canonical_json(build_context(snapshot))
        context_hash = sha256_hex(context_json)
        known = snapshot.known_ids()
        run = _Run(messages=[Message("user", text=_task_message(context_json))], tools=ReadOnlyTools(snapshot))

        def finish(
            outcome: Outcome,
            attempts: int,
            items: tuple[ProposedAction, ...] = (),
            errors: list[str] | None = None,
            raw: str | None = None,
        ) -> PlanResult:
            proposals, cited = self._to_proposals(items, context_hash, now)
            record = PlanRecord(
                model=self._llm.model,
                prompt_name=self._prompt.name,
                prompt_hash=self._prompt.sha256,
                context_hash=context_hash,
                temperature=TEMPERATURE,
                outcome=outcome,
                attempts=attempts,
                llm_calls=run.llm_calls,
                tool_calls=tuple(run.traces),
                evidence_ids=tuple(run.evidence),
                validation_errors=tuple(errors or ()),
                raw_output=None if raw is None else raw[:MAX_RAW_CHARS],
            )
            return PlanResult(proposals=proposals, evidence=cited, record=record)

        attempts = 1
        errors: list[str] = []
        try:
            text = self._converse(run)
            items, errors = validate_reply(text, known, frozenset(run.evidence))
            if errors:  # one retry, tools off, with a value-free list of what failed
                run.messages += [
                    Message("assistant", text=text or "(empty reply)"),
                    Message("user", text=_retry_message(errors)),
                ]
                attempts = 2
                text = self._call(run, tools_on=False).text or ""
                items, errors = validate_reply(text, known, frozenset(run.evidence))
                if errors:
                    return finish("invalid_output", attempts, errors=errors, raw=text)
            return finish("ok" if items else "ok_empty", attempts, items, raw=text)
        except LLMError:  # keep what the first attempt got wrong: the audit record should show it
            return finish("llm_error", attempts, errors=[*errors, "llm_error: the model could not be reached"])

    # ---- model conversation ----------------------------------------------------------------
    def _call(self, run: _Run, *, tools_on: bool) -> LLMResponse:
        run.llm_calls += 1
        specs = run.tools.specs() if tools_on else ()
        return self._llm.complete(system=self._prompt.text, messages=run.messages, tools=specs, temperature=TEMPERATURE)

    def _converse(self, run: _Run) -> str:
        """Answer tool calls until the model replies with text (or the budget is spent)."""
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            response = self._call(run, tools_on=round_no < MAX_TOOL_ROUNDS)
            if not response.tool_calls or round_no == MAX_TOOL_ROUNDS:
                return response.text or ""
            run.messages.append(Message("assistant", text=response.text, tool_calls=response.tool_calls))
            results = tuple(self._answer(run, call.name, call.arguments, call.id) for call in response.tool_calls)
            run.messages.append(Message("user", tool_results=results))
        return ""  # unreachable: the last round returns above

    def _answer(self, run: _Run, name: str, arguments: object, call_id: str) -> ToolResult:
        args = dict(arguments) if isinstance(arguments, dict) else {}
        if len(run.traces) >= MAX_TOOL_CALLS:
            payload: dict[str, JsonValue] = {
                "error": {"type": "tool_budget_exhausted", "message": "no more tool calls"}
            }
        else:
            payload = run.tools.call(name, args)
        failed = "error" in payload
        evidence_id = None if failed else f"ev-{len(run.evidence) + 1}"
        if evidence_id is not None:
            run.evidence.append(evidence_id)
        wrapped: dict[str, JsonValue] = {
            "tool": name[:60],
            **({"evidence_id": evidence_id} if evidence_id else {}),
            "result": payload,
        }
        content = canonical_json(wrapped)
        run.traces.append(
            ToolTrace(
                name=name[:60],
                arguments_json=_safe_json(args),
                evidence_id=evidence_id,
                result_sha256=sha256_hex(content),
            )
        )
        return ToolResult(tool_call_id=call_id, content=content)

    # ---- output ----------------------------------------------------------------------------
    @staticmethod
    def _to_proposals(
        items: tuple[ProposedAction, ...], context_hash: str, now: datetime
    ) -> tuple[tuple[Proposal, ...], dict[str, tuple[str, ...]]]:
        proposals: list[Proposal] = []
        cited: dict[str, tuple[str, ...]] = {}
        for index, item in enumerate(items):
            body = item.model_dump(mode="json")
            digest = sha256_hex(f"{context_hash}|{index}|{canonical_json(body)}")[:20]
            proposal = Proposal(
                id=f"prp-{digest}",
                action_type=ActionType(item.action_type),
                params=item.params.model_dump(mode="json"),
                rationale=item.rationale,
                confidence=item.confidence,
                created_at=now,
            )
            proposals.append(proposal)
            cited[proposal.id] = tuple(item.evidence_ids)
        return tuple(proposals), cited


def _task_message(context_json: str) -> str:
    return (
        "Plan treasury actions for this business. The context below is complete; use the tools for detail. "
        "Reply with one JSON object only.\n\n<context>\n" + context_json + "\n</context>"
    )


def _retry_message(errors: list[str]) -> str:
    listed = "\n".join(f"- {line}" for line in errors)
    return (
        "Your reply was not valid. These fields failed (paths and codes only):\n"
        f"{listed}\nReply again with ONE corrected JSON object and nothing else. You cannot call tools now."
    )


def _safe_json(value: object) -> str:
    try:
        return canonical_json(value)[:2000]
    except (TypeError, ValueError):
        return '"<unserialisable arguments>"'
