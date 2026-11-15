"use client";

import { loadBalances } from "@/lib/queries";
import { useApi } from "@/lib/useApi";
import { formatDate, formatMoney } from "@/lib/format";
import { Async, EmptyState, Skeleton } from "./States";

export function BalanceCards({ businessId }: { businessId: string }) {
  const remote = useApi(() => loadBalances(businessId), [businessId]);
  return (
    <section className="section" aria-labelledby="balances-h">
      <h2 id="balances-h">Balances</h2>
      <Async
        remote={remote}
        isEmpty={(d) => d.balances.length === 0}
        empty={<EmptyState title="No balances yet">Balances appear once the worker has copied them from Airwallex.</EmptyState>}
        skeleton={<div className="grid"><Skeleton height={96} label="Loading balances" /><Skeleton height={96} label="Loading balances" /></div>}
      >
        {(d) => (
          <ul className="grid list" style={{ listStyle: "none", padding: 0 }}>
            {d.balances.map((b) => (
              <li key={b.available.currency} className="card balance" style={{ display: "block" }}>
                <div className="code">{b.available.currency}</div>
                <div className="amount">{formatMoney(b.available)}</div>
                <div className="hint">as of {formatDate(b.as_of)}</div>
              </li>
            ))}
          </ul>
        )}
      </Async>
    </section>
  );
}
