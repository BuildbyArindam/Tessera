"use client";

import { loadObligations } from "@/lib/queries";
import { useApi } from "@/lib/useApi";
import { describeDue, formatDate, formatMoney } from "@/lib/format";
import { Async, EmptyState, Skeleton } from "./States";

export function Obligations({ businessId, days }: { businessId: string; days: number }) {
  const remote = useApi(() => loadObligations(businessId, days), [businessId, days]);
  return (
    <section className="section" aria-labelledby="obl-h">
      <h2 id="obl-h">Upcoming obligations · next {days} days</h2>
      <Async
        remote={remote}
        isEmpty={(d) => d.items.length === 0}
        empty={<EmptyState title="Nothing due">No open invoices or payables fall in this window.</EmptyState>}
        skeleton={<Skeleton height={220} label="Loading obligations" />}
      >
        {(d) => (
          <div className="card">
            <ul className="list">
              {d.items.map((o) => (
                <li key={o.id}>
                  <span>
                    <span className="who">{o.counterparty ?? "Unknown"}</span>{" "}
                    <span className={`badge ${o.kind === "RECEIVABLE" ? "info" : "medium"}`}>
                      {o.kind === "RECEIVABLE" ? "↓ Money in" : "↑ Money out"}
                    </span>
                    <div className="when">
                      {formatDate(o.due_on)} · {describeDue(o.days_until_due)}
                    </div>
                  </span>
                  <span className="num">{formatMoney(o.amount)}</span>
                </li>
              ))}
            </ul>
            <p className="hint">Measured from {formatDate(d.as_of)}, the date of the latest forecast.</p>
          </div>
        )}
      </Async>
    </section>
  );
}
