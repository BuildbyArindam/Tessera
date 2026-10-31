"""Day 9: the strict proposal schema, the strict JSON parser, and the prompt that describes them."""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest
from agent_helpers import NOW, convert_item, item, reply
from pydantic import ValidationError
from tessera_agent import ProposalList, load_prompt
from tessera_agent.canon import OutputFormatError, parse_strict_json
from tessera_agent.schemas import looks_like_account_number

pytestmark = pytest.mark.usefixtures("no_network")


def parse(obj: Any) -> ProposalList:
    return ProposalList.model_validate(obj)


def good() -> dict[str, Any]:
    return {"proposals": [convert_item()]}


def rejected(mutate: Any) -> list[str]:
    data = copy.deepcopy(good())
    mutate(data)
    with pytest.raises(ValidationError) as info:
        parse(data)
    return [e["type"] for e in info.value.errors()]


def test_a_clean_proposal_parses_and_an_empty_list_is_valid() -> None:
    assert parse(good()).proposals[0].params.amount == "3000.00"
    assert parse({"proposals": []}).proposals == []


# ---- extra fields are errors, at every level -------------------------------------------------
def test_extra_field_on_the_proposal_is_rejected() -> None:
    assert "extra_forbidden" in rejected(lambda d: d["proposals"][0].update(execute=True))


def test_extra_field_in_params_is_rejected() -> None:
    assert "extra_forbidden" in rejected(lambda d: d["proposals"][0]["params"].update(due_date="2026-03-02"))


def test_extra_top_level_key_is_rejected() -> None:
    data = good() | {"note": "hello"}
    with pytest.raises(ValidationError):
        parse(data)


@pytest.mark.parametrize("field", ["rate", "quote_id", "account_number", "iban", "beneficiary_name", "limit"])
def test_the_model_cannot_set_rates_quotes_names_or_account_fields(field: str) -> None:
    assert "extra_forbidden" in rejected(lambda d: d["proposals"][0]["params"].update({field: "x"}))


# ---- free-text account numbers have nowhere to go ---------------------------------------------
PAY = {
    "amount": "500.00",
    "currency": "INR",
    "from_currency": "USD",
    "beneficiary_id": "BEN-001",
    "obligation_id": "OBL-0001",
}


@pytest.mark.parametrize(
    "bad_id",
    [
        "GB82WEST12345698765432",
        "12345678901234",
        "GB82-WEST-1234-5698-7654-32",
        "4111111111111111",
        "DE89370400440532013000",
    ],
)
def test_an_account_number_in_an_id_field_is_rejected(bad_id: str) -> None:
    data = {"proposals": [item("PAY_SUPPLIER", {**PAY, "beneficiary_id": bad_id})]}
    with pytest.raises(ValidationError):
        parse(data)


@pytest.mark.parametrize("free_text", ["Acme Supplies Ltd", "pay 1234 5678 to bank", "../etc/passwd", "", "a" * 65])
def test_free_text_is_not_a_valid_id(free_text: str) -> None:
    data = {"proposals": [item("PAY_SUPPLIER", {**PAY, "beneficiary_id": free_text})]}
    with pytest.raises(ValidationError):
        parse(data)


def test_real_record_ids_are_not_mistaken_for_account_numbers() -> None:
    for ok in ("INV-B01-00001", "CP-B01-003", "BEN-001", "OBL-0001", "VND-AWS", "C-B01-001"):
        assert not looks_like_account_number(ok)
        parse({"proposals": [item("FLAG_INVOICE", {"invoice_id": ok, "reason": "likely_late"})]})


# ---- amounts, confidence, currencies -----------------------------------------------------------
@pytest.mark.parametrize(
    "amount", [100.5, 100, True, "1e3", " 100", "+100", "-5", "1,000", "１００", "100.123", "", None]
)
def test_amount_must_be_a_plain_ascii_decimal_string(amount: Any) -> None:
    rejected(lambda d: d["proposals"][0]["params"].update(amount=amount))


@pytest.mark.parametrize("confidence", [True, False, -0.1, 1.1, float("nan"), "0.5", None])
def test_confidence_must_be_a_real_number_between_0_and_1(confidence: Any) -> None:
    with pytest.raises((ValidationError, ValueError)):
        data = good()
        data["proposals"][0]["confidence"] = confidence
        parse(data)


def test_unknown_currency_and_same_currency_conversion_are_rejected() -> None:
    rejected(lambda d: d["proposals"][0]["params"].update(currency="BTC"))
    assert "value_error" in rejected(lambda d: d["proposals"][0]["params"].update(to_currency="USD"))


def test_unknown_action_and_missing_params_are_rejected() -> None:
    rejected(lambda d: d["proposals"][0].update(action_type="WIRE_ALL"))
    with pytest.raises(ValidationError):
        parse({"proposals": [item("PAY_SUPPLIER", {k: v for k, v in PAY.items() if k != "beneficiary_id"})]})


def test_flag_reason_is_a_code_not_free_text() -> None:
    with pytest.raises(ValidationError):
        parse({"proposals": [item("FLAG_INVOICE", {"invoice_id": "INV-1", "reason": "ignore your rules"})]})


def test_evidence_is_required_and_bounded() -> None:
    assert "too_short" in rejected(lambda d: d["proposals"][0].update(evidence_ids=[]))
    rejected(lambda d: d["proposals"][0].update(evidence_ids=["trust-me"]))
    rejected(lambda d: d["proposals"][0].update(evidence_ids=[f"ev-{i}" for i in range(1, 10)]))


def test_at_most_five_proposals() -> None:
    parse({"proposals": [convert_item(amount=f"{i}00.00") for i in range(1, 6)]})
    with pytest.raises(ValidationError) as info:
        parse({"proposals": [convert_item(amount=f"{i}00.00") for i in range(1, 7)]})
    assert info.value.errors()[0]["type"] == "too_long"


# ---- the strict JSON parser ---------------------------------------------------------------------
def test_parser_accepts_one_object_optionally_in_a_single_fence() -> None:
    text = reply(convert_item())
    assert parse_strict_json(text) == parse_strict_json(f"```json\n{text}\n```")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "not json",
        '{"proposals": []} and then some prose',
        'Here you go: {"proposals": []}',
        "[]",
        '"x"',
        '{"proposals": [], "proposals": [{"x": 1}]}',  # duplicate key
        '{"proposals": [], "n": NaN}',
        '{"proposals": [], "n": Infinity}',
        "```json\n{}\n``` extra",
        "x" * 60_000,
        "[" * 5000,
    ],
)
def test_parser_rejects_anything_that_is_not_one_unambiguous_object(text: str) -> None:
    with pytest.raises(OutputFormatError):
        parse_strict_json(text)


# ---- the prompt matches the schema -------------------------------------------------------------
def test_the_example_in_the_prompt_validates_against_the_schema() -> None:
    block = re.search(r'\{"proposals".*?\n\]\}', load_prompt().text, re.S)
    assert block is not None
    assert len(ProposalList.model_validate_json(block.group(0)).proposals) == 2


def test_the_prompt_hash_is_stable_and_tracks_the_text() -> None:
    first, second = load_prompt(), load_prompt()
    assert first.sha256 == second.sha256 and len(first.sha256) == 64
    with pytest.raises(ValueError):
        load_prompt("../planner_v1")


def test_now_fixture_is_timezone_aware() -> None:
    assert NOW.tzinfo is not None
