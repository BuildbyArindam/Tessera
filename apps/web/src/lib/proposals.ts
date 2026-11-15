import type { Currency, Money, Proposal } from "@/api/client";

export type Status = Proposal["status"];
export type Tab = "pending" | "decided" | "blocked";

export const TABS: readonly { id: Tab; label: string }[] = [
  { id: "pending", label: "Needs approval" },
  { id: "decided", label: "Decided" },
  { id: "blocked", label: "Blocked" },
];

/** Which tab a proposal belongs to. BLOCKED is terminal and never offers an approve path. */
export function tabOf(status: Status): Tab {
  if (status === "PENDING_APPROVAL") return "pending";
  if (status === "BLOCKED") return "blocked";
  return "decided";
}

/**
 * What each status means, in words that never overstate. Only SETTLED says the money moved. APPROVED means a person
 * (or policy) said yes and the worker has not yet re-checked and submitted it; SUBMITTED means sent, not settled.
 */
export const STATUS_LABEL: Record<Status, string> = {
  PENDING_APPROVAL: "Needs your approval",
  APPROVED: "Approved · waiting for the worker",
  REJECTED: "Rejected",
  BLOCKED: "Blocked by policy",
  SUBMITTED: "Submitted · not settled yet",
  SETTLED: "Executed · settled",
  FAILED: "Failed",
};

export const STATUS_TONE: Record<Status, "low" | "medium" | "high" | "info"> = {
  PENDING_APPROVAL: "medium",
  APPROVED: "info",
  REJECTED: "info",
  BLOCKED: "high",
  SUBMITTED: "info",
  SETTLED: "low",
  FAILED: "high",
};

/** Statuses the worker is still moving, so the list keeps refreshing while any proposal is in one. */
export const IN_FLIGHT: readonly Status[] = ["APPROVED", "SUBMITTED"];

export const ACTION_LABEL: Record<string, string> = {
  CONVERT: "Convert",
  HOLD: "Hold",
  PAY_SUPPLIER: "Pay supplier",
  TOPUP_CARD: "Top up card",
  FLAG_INVOICE: "Flag invoice",
};

const CURRENCIES: readonly string[] = ["USD", "EUR", "GBP", "INR", "SGD", "AUD"];
const AMOUNT = /^[0-9]{1,17}(\.[0-9]{1,2})?$/;

/** `params` values arrive as `unknown`. Narrow each one at runtime; anything that does not fit is simply absent. */
export function paramString(p: Proposal, key: string): string | undefined {
  const v = p.params[key];
  return typeof v === "string" && v.length > 0 && v.length <= 128 ? v : undefined;
}

export function paramCurrency(p: Proposal, key: string): Currency | undefined {
  const v = paramString(p, key);
  return v !== undefined && CURRENCIES.includes(v) ? (v as Currency) : undefined;
}

/** The proposal's own amount, in the currency named by `currencyKey`. Amounts stay decimal strings. */
export function paramMoney(p: Proposal, currencyKey = "currency"): Money | undefined {
  const amount = paramString(p, "amount");
  const currency = paramCurrency(p, currencyKey);
  return amount !== undefined && AMOUNT.test(amount) && currency !== undefined ? { amount, currency } : undefined;
}

/**
 * The currency whose forecast sits behind this proposal: the one the business gives up. A payout is funded from
 * `from_currency`; a conversion or hold sells `currency`; a card top-up loads `currency`. A flag has none.
 */
export function forecastCurrency(p: Proposal): Currency | undefined {
  if (p.action_type === "PAY_SUPPLIER") return paramCurrency(p, "from_currency");
  if (p.action_type === "FLAG_INVOICE") return undefined;
  return paramCurrency(p, "currency");
}

export function replaceProposal(list: Proposal[], next: Proposal): Proposal[] {
  return list.map((p) => (p.id === next.id ? next : p));
}
