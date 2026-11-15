"use client";

import { loadRiskyInvoices } from "@/lib/queries";
import { useApi } from "@/lib/useApi";
import { formatDate, formatMoney, formatPercent } from "@/lib/format";
import { RISK_LABEL, riskLevel } from "@/lib/risk";
import type { InvoiceRisk as InvoiceRiskRow } from "@/api/client";
import { Async, EmptyState, Skeleton } from "./States";

export function RiskBadge({ risk }: { risk: InvoiceRiskRow["late_risk"] }) {
  if (!risk) return <span className="badge info">Not scored</span>;
  const level = riskLevel(risk.p_late, risk.already_late);
  const icon = level === "low" ? "●" : level === "medium" ? "▲" : "■";
  return (
    <span className={`badge ${level}`}>
      <span aria-hidden="true">{icon}</span>
      {RISK_LABEL[level]}
      {level === "late" ? "" : ` · ${formatPercent(risk.p_late)}`}
    </span>
  );
}

export function InvoiceRisk({ businessId }: { businessId: string }) {
  const remote = useApi(() => loadRiskyInvoices(businessId), [businessId]);
  return (
    <section className="section" aria-labelledby="risk-h">
      <h2 id="risk-h">Open invoices · late-payment risk</h2>
      <p className="hint">Probability the customer pays more than 7 days after the due date, riskiest first.</p>
      <div style={{ height: 12 }} />
      <Async
        remote={remote}
        isEmpty={(d) => d.length === 0}
        empty={<EmptyState title="No open invoices">Everything has been paid.</EmptyState>}
        skeleton={<Skeleton height={220} label="Loading invoices" />}
      >
        {(rows) => (
          <div className="card" style={{ overflowX: "auto" }} tabIndex={0} role="region" aria-label="Open invoices, scrollable">
            <table>
              <caption className="sr-only">Open invoices ranked by late-payment risk</caption>
              <thead>
                <tr>
                  <th scope="col">Invoice</th>
                  <th scope="col">Customer</th>
                  <th scope="col">Due</th>
                  <th scope="col" className="r">Amount</th>
                  <th scope="col">Risk</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((i) => (
                  <tr key={i.id}>
                    <th scope="row">{i.id}</th>
                    <td>{i.customer_id}</td>
                    <td>{formatDate(i.due_on)}</td>
                    <td className="r">{formatMoney(i.amount)}</td>
                    <td><RiskBadge risk={i.late_risk} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Async>
    </section>
  );
}
