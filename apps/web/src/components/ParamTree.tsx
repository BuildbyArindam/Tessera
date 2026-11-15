import { Fragment } from "react";
import type { Currency } from "@/api/client";
import { formatMoney } from "@/lib/format";

const CURRENCIES = ["USD", "EUR", "GBP", "INR", "SGD", "AUD"];
const MAX_DEPTH = 6;

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** `{amount, currency}` and nothing else: how the API writes money, so show it as money rather than as two fields. */
function asMoney(v: unknown): { amount: string; currency: Currency } | null {
  if (!isRecord(v) || Object.keys(v).length !== 2) return null;
  const { amount, currency } = v;
  if (typeof amount !== "string" || !/^-?[0-9]+(\.[0-9]+)?$/.test(amount)) return null;
  if (typeof currency !== "string" || !CURRENCIES.includes(currency)) return null;
  return { amount, currency: currency as Currency };
}

/** Render rule parameters (arbitrary JSON from the ruleset) read-only. Decimal strings stay exactly as configured. */
export function ParamTree({ value, depth = 0 }: { value: unknown; depth?: number }) {
  const money = asMoney(value);
  if (money) return <span className="num">{formatMoney(money)}</span>;
  if (depth >= MAX_DEPTH) return <span className="hint">…</span>;

  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="hint">none</span>;
    return <span>{value.map((v, i) => (i > 0 ? `, ${String(v)}` : String(v)))}</span>;
  }
  if (isRecord(value)) {
    const entries = Object.entries(value);
    if (entries.length === 0) return <span className="hint">none</span>;
    return (
      <dl className="params">
        {entries.map(([k, v]) => (
          <Fragment key={k}>
            <dt>{k}</dt>
            <dd>
              <ParamTree value={v} depth={depth + 1} />
            </dd>
          </Fragment>
        ))}
      </dl>
    );
  }
  return <span className="mono">{value === null ? "null" : String(value)}</span>;
}
