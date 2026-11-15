import type { Money, Proposal, Simulation } from "@/api/client";
import { formatMoney, formatPercent } from "./format";
import { paramMoney, paramString } from "./proposals";

/**
 * The plain-language sentence for a proposal, filled in from STRUCTURED fields only.
 *
 * Why a template: a number in this sentence can only come from a field the API returned, formatted by `formatMoney`.
 * The model's own `rationale` is free text and never feeds this function; the card shows it separately, labelled as
 * unverified. The template also writes no figure of its own (no "5%" or "30 days"): every digit is the API's.
 * The only derived number is the confidence score shown as a whole percent, which is a rounding of a payload value.
 */
export function explainProposal(p: Proposal): string {
  return [actionSentence(p), simulationSentence(p), confidenceSentence(p)].filter((s) => s !== "").join(" ");
}

const FLAG_REASON: Record<string, string> = {
  likely_late: "likely to be paid late",
  dispute_risk: "at risk of a dispute",
  amount_mismatch: "showing an amount mismatch",
  other: "needing a review",
};

function actionSentence(p: Proposal): string {
  const obligation = paramString(p, "obligation_id");
  const forObligation = obligation ? ` for obligation ${obligation}` : "";
  const amount = paramMoney(p);
  const shown = amount ? formatMoney(amount) : undefined;
  switch (p.action_type) {
    case "CONVERT": {
      const to = paramString(p, "to_currency");
      return shown && to ? `Convert ${shown} into ${to} now${forObligation}.` : "Convert funds now (details unavailable).";
    }
    case "HOLD": {
      const to = paramString(p, "to_currency");
      return shown && to ? `Hold ${shown} instead of converting it into ${to}${forObligation}.` : "Hold funds (details unavailable).";
    }
    case "PAY_SUPPLIER": {
      const who = paramString(p, "beneficiary_id");
      const from = paramString(p, "from_currency");
      const funded = from && amount && from !== amount.currency ? `, funded from the ${from} balance` : "";
      return shown && who ? `Pay ${shown} to beneficiary ${who}${forObligation}${funded}.` : "Pay a supplier (details unavailable).";
    }
    case "TOPUP_CARD": {
      const vendor = paramString(p, "vendor_id");
      return shown && vendor ? `Top up the virtual card for vendor ${vendor} with ${shown}.` : "Top up a card (details unavailable).";
    }
    case "FLAG_INVOICE": {
      const invoice = paramString(p, "invoice_id");
      const reason = FLAG_REASON[paramString(p, "reason") ?? ""];
      return invoice && reason ? `Flag invoice ${invoice} as ${reason}.` : "Flag an invoice (details unavailable).";
    }
    default:
      return "Proposed an action this screen has no template for.";
  }
}

/** What the simulator compares the proposal with: a HOLD against converting now, the rest against the due date. */
const COMPARATOR: Record<string, string> = {
  CONVERT: "converting on the due date",
  PAY_SUPPLIER: "converting on the due date",
  HOLD: "converting now",
};

const isNegative = (m: Money) => m.amount.startsWith("-");
const magnitude = (m: Money): Money => ({ ...m, amount: m.amount.replace(/^-/, "") });
const isZero = (m: Money) => /^-?0+(\.0+)?$/.test(m.amount);

function simulationSentence(p: Proposal): string {
  const sim: Simulation | null | undefined = p.decision?.simulation;
  const against = COMPARATOR[p.action_type];
  if (!sim || !against) return "";
  const paths = new Intl.NumberFormat("en").format(sim.n_paths);
  const saving = sim.expected_saving;
  const result = isZero(saving)
    ? `expected to cost the same as ${against}`
    : isNegative(saving)
      ? `expected to cost ${formatMoney(magnitude(saving))} more than ${against}`
      : `expected to save ${formatMoney(saving)} compared with ${against}`;
  const tail = isZero(sim.worst_case_loss)
    ? "no loss appears in the worst-case tail."
    : `in the worst-case tail the loss is ${formatMoney(sim.worst_case_loss)} or more.`;
  return `Across ${paths} simulated rate paths it is ${result}; ${tail}`;
}

function confidenceSentence(p: Proposal): string {
  return `The agent's own confidence score is ${formatPercent(p.confidence)}.`;
}
