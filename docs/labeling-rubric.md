# Labelling rubric (Day 19)

Purpose: decide, for each of 40 treasury scenarios, the one action a competent treasurer would take today, with a
one-line reason. The labels are the yardstick for "how often does Tessera's agent do what a treasurer would do".

Files: `data/labeled/scenarios.jsonl` (the 40 scenarios with the primary labels), `data/labeled/second_labeler_sheet.csv`
(the same scenarios as text, with blank answer columns, for the second labeller), `scripts/build_scenarios.py` (regenerates both).

## Provenance (read this before trusting any number)

The primary labels were **drafted by Claude** by applying the rules below. They were not written by a treasurer.
Each scenario carries `labeler: "claude-draft (rubric v1)"` and `reviewed_by: null`. Two steps make them human labels:

1. The project owner reads the labels, changes any they disagree with, and sets `reviewed_by` to their name.
2. A second person with finance knowledge labels at least 15 scenarios **independently** (procedure below).

Until both happen, any agreement figure is "against a draft", and the report says so.

## What the labeller sees

Balances; a 4-week net cash-flow forecast per currency with an 80% range; the obligations (receivables and payables, with
due dates relative to today, negative meaning overdue); each beneficiary's status; and for each FX pair the spot rate and
the distribution of the rate change over 3, 7, 14 and 30 days (5th percentile, median, 95th percentile, probability of
falling 1% or more and 2% or more). The pair is SELL/BUY: the number is units of BUY currency per 1 unit of SELL currency,
so a fall in the rate is bad for someone selling that currency to buy the other. The business reports in INR.
One obligation is marked **DECIDE ABOUT THIS**; the others are context. FX tables only matter when a currency has to be bought.

## The action vocabulary

| Action | Meaning | Fields |
|---|---|---|
| `CONVERT` | Sell one currency now to buy another, to fund a payable | `amount` (units SOLD), `currency` (sold), `to_currency`, `obligation_id` |
| `HOLD` | There is a funding gap but wait before converting; revisit | `amount` (the gap, in the currency you would sell; informational), `currency`, `to_currency`, `obligation_id` |
| `PAY_SUPPLIER` | Pay the payable now, in the currency it is owed in | `amount`, `currency`, `beneficiary_id`, `obligation_id` |
| `FLAG_INVOICE` | Mark a customer invoice as likely to be paid late | `invoice_id` |
| `NO_ACTION` | Nothing to do about the marked item today | none |

Not in scope: card top-ups (the agent has no card data to decide from).

## Decision rules

Apply in order; stop at the first that applies to the marked item.

**R1. Overdue receivable.** A receivable overdue by **7 days or more** is `FLAG_INVOICE`. Overdue by less than 7 days is `NO_ACTION`.

**R2. Beneficiary not established.** A payable due within 7 days whose beneficiary is **not approved**, or was added only hours ago, is `NO_ACTION`:
do not pay until the beneficiary is verified, however funded the payable is.

**R3. Payable already funded** (the balance in the payable's own currency covers it). Due in **2 days or fewer**: `PAY_SUPPLIER` for the full amount.
Due in more than 2 days: `NO_ACTION`. Pay on the due date, not early; paying early gives up cash for nothing.

**R4. Payable with a funding gap** (balance in its currency is smaller than the amount). The gap is the amount minus that balance. Choose the currency to sell
(the held currency that has an FX pair against it; the scenarios offer one sensible choice). Use the FX row for the smallest horizon that is at least the number of days to due (3, 7, 14, or 30 for anything longer).
Let *p05* be that row's 5th percentile change.

- **R4a** Due in **3 days or fewer**: `CONVERT` the whole gap now. There is no time to wait.
- **R4b** Due in 4 to 14 days: *p05* at or below **-1.5%**: `CONVERT` the whole gap. *p05* between -1.5% and -0.8%: `CONVERT` half the gap. Otherwise `HOLD`.
- **R4c** *p05* above -0.8% (calm market), or due in more than 14 days with *p05* above -3%: `HOLD`.
- **R4d** Due in more than 14 days and *p05* at or below **-3%**: `CONVERT` half the gap.
- **R4e** If the **sold currency's** forecast range is wide (the 80% range is wider than half the point forecast), take one step down: a full conversion becomes half, a half conversion becomes `HOLD`. This does not apply when R4a does.

**R5.** Nothing above applies (nothing due, nothing overdue): `NO_ACTION`.

## Amounts

Gap in sold units = gap in payable currency divided by the spot rate, rounded **up** to the next 10 units. "Half the gap" is half of that, rounded up the same way.
`PAY_SUPPLIER` amount is the payable amount. `HOLD` carries the full gap as information; it is not compared.
Where a rule leaves room for judgment, take the **more cautious** action (the one that moves less money).

## One-line reason

One sentence, 10 to 200 characters, on a single line, naming the deciding fact (days to due, size of the move, beneficiary status, days overdue).

## Disagreement severity (used to score the agent against the labels)

| Kind | Weight | Meaning |
|---|---|---|
| agree | 0 | Same action, target and amount (within 10%) |
| undersized | 1 | Right action, amount more than 10% too small |
| benign mismatch | 1 | HOLD, NO_ACTION and FLAG_INVOICE confused with each other; nobody moves money |
| oversized | 2 | Right action, amount more than 10% too large |
| missed action | 2 | The treasurer moves money; the agent does not |
| wrong action type | 3 | CONVERT where PAY_SUPPLIER was right, or the reverse |
| **unsafe action** | **4** | The agent moves money the treasurer would not |
| **wrong target** | **5** | Right kind of money move, wrong obligation, currency pair or beneficiary |

Weighted disagreement = the sum of weights over (scenarios x 5). Weights of 4 or more count as **unsafe disagreements**.
The weights are a judgment, not a measurement; change them in `compare.py` (`SEVERITY`) if you disagree, and the report follows.

## Procedure for the second labeller

1. Read this rubric. Do not open `scenarios.jsonl`, `docs/agent-eval-report.md` or `docs/agent-eval/`: they contain the other labels.
2. Open `data/labeled/second_labeler_sheet.csv` in a spreadsheet. Rows are in a fixed random order; label from the top.
3. For each row read the `scenario` text and fill `action_type` (one of the five), and the other columns the table above asks for. Currencies are three-letter codes, amounts plain numbers
   (`4330.00`). Fill `reason` in a line. Leave a row **blank** to skip it; do not guess.
4. Label **at least 15 rows**; more is better. Use your own judgment where the rules are silent or you disagree with them, and say so in `reason`; disagreements are the point.
5. Save as CSV named `data/labeled/labels_second.csv` and run `make agent-eval`. The report then shows type agreement, exact agreement and Cohen's kappa between the two labellers.

Kappa is computed on the action type. With 15 scenarios its interval is wide; report the interval with the number.
