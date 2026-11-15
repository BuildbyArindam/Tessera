"use client";

import { useId, useState } from "react";
import { ApiError, type Forecast, type Proposal } from "@/api/client";
import { explainProposal } from "@/lib/explain";
import { formatDateTime, formatMoney, formatPercent } from "@/lib/format";
import { intervalsFor } from "@/lib/forecast";
import { decideProposal } from "@/lib/mutations";
import { ACTION_LABEL, STATUS_LABEL, STATUS_TONE, forecastCurrency, paramMoney } from "@/lib/proposals";
import type { Remote } from "@/lib/useApi";

const VERDICT_TONE = { APPROVE: "low", ESCALATE: "medium", BLOCK: "high" } as const;

export function ProposalCard({
  proposal,
  forecasts,
  onChanged,
}: {
  proposal: Proposal;
  forecasts: Remote<Forecast[]>;
  onChanged: (updated: Proposal) => void;
}) {
  const titleId = useId();
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const amount = paramMoney(proposal);

  async function decide(choice: "approve" | "reject") {
    setBusy(choice);
    setError(null);
    try {
      onChanged(await decideProposal(proposal.id, choice)); // render what the API says, nothing assumed
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the API. Nothing was changed.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <article className="card proposal" aria-labelledby={titleId} data-proposal-id={proposal.id} data-status={proposal.status}>
      <header className="proposal-head">
        <div>
          <h3 id={titleId}>{ACTION_LABEL[proposal.action_type] ?? proposal.action_type}</h3>
          <p className="hint mono">{proposal.id}</p>
        </div>
        {amount ? <span className="amount num">{formatMoney(amount)}</span> : null}
        <span className={`badge ${STATUS_TONE[proposal.status]}`} data-testid="status">
          {STATUS_LABEL[proposal.status]}
        </span>
      </header>

      <section aria-label="Summary">
        <p className="hint">Summary, written from the figures below</p>
        <p className="explanation" data-testid="explanation">
          {explainProposal(proposal)}
        </p>
      </section>

      <div className="evidence">
        <ForecastBlock proposal={proposal} forecasts={forecasts} />
        <SimulationBlock proposal={proposal} />
        <PolicyBlock proposal={proposal} />
      </div>

      <details>
        <summary>The agent&apos;s own words (unverified)</summary>
        <p className="untrusted">{proposal.rationale}</p>
        <p className="hint">
          Written by the model, so it can be wrong. Decide from the figures and the policy verdict, not from this text.
        </p>
      </details>

      {proposal.approval ? (
        <p className="hint" data-testid="approval-record">
          {proposal.approval.decision === "APPROVED" ? "Approved" : "Rejected"} by {proposal.approval.actor} on{" "}
          {formatDateTime(proposal.approval.decided_at)}
          {proposal.approval.note ? ` · note: ${proposal.approval.note}` : ""}
        </p>
      ) : null}

      {proposal.status === "PENDING_APPROVAL" ? (
        <footer className="actions">
          <button type="button" className="primary" disabled={busy !== null} aria-busy={busy === "approve"} onClick={() => void decide("approve")}>
            {busy === "approve" ? "Approving…" : "Approve"}
          </button>
          <button type="button" disabled={busy !== null} aria-busy={busy === "reject"} onClick={() => void decide("reject")}>
            {busy === "reject" ? "Rejecting…" : "Reject"}
          </button>
          <span className="hint">Approving does not move money. The worker re-checks policy first.</span>
        </footer>
      ) : null}
      {error ? (
        <p className="state error" role="alert">
          {error}
        </p>
      ) : null}
    </article>
  );
}

function ForecastBlock({ proposal, forecasts }: { proposal: Proposal; forecasts: Remote<Forecast[]> }) {
  const currency = forecastCurrency(proposal);
  let body;
  if (!currency) body = <p className="hint">No forecast applies to this action.</p>;
  else if (forecasts.status === "loading") body = <p className="hint">Loading forecast…</p>;
  else if (forecasts.status === "error") body = <p className="hint">Forecast unavailable right now.</p>;
  else {
    const rows = intervalsFor(forecasts.data, currency);
    body =
      rows.length === 0 ? (
        <p className="hint">No {currency} forecast is stored.</p>
      ) : (
        <table>
          <caption className="sr-only">{currency} net cash flow forecast intervals</caption>
          <thead>
            <tr>
              <th scope="col">Window</th>
              <th scope="col" className="r">Low</th>
              <th scope="col" className="r">Point</th>
              <th scope="col" className="r">High</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.days}>
                <th scope="row">{r.days} days</th>
                <td className="r">{formatMoney(r.lower, { compact: true })}</td>
                <td className="r">{formatMoney(r.point, { compact: true })}</td>
                <td className="r">{formatMoney(r.upper, { compact: true })}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td colSpan={4} className="hint">
                {formatPercent(rows[0]?.coverage ?? 0)} target coverage · as of {rows[0]?.asOf}
              </td>
            </tr>
          </tfoot>
        </table>
      );
  }
  return (
    <section aria-label="Forecast interval">
      <h4>Forecast behind it{currency ? ` (${currency})` : ""}</h4>
      {body}
    </section>
  );
}

function SimulationBlock({ proposal }: { proposal: Proposal }) {
  const sim = proposal.decision?.simulation;
  return (
    <section aria-label="Simulation">
      <h4>Simulation</h4>
      {sim ? (
        <dl className="facts">
          <dt>Expected saving</dt>
          <dd className="num" data-testid="expected-saving">{formatMoney(sim.expected_saving, { sign: true })}</dd>
          <dt>Worst-case loss (5th percentile)</dt>
          <dd className="num" data-testid="worst-case-loss">{formatMoney(sim.worst_case_loss)}</dd>
          <dt>Rate paths</dt>
          <dd className="num">{new Intl.NumberFormat("en").format(sim.n_paths)}</dd>
        </dl>
      ) : (
        <p className="hint">No simulation was recorded for this proposal.</p>
      )}
    </section>
  );
}

function PolicyBlock({ proposal }: { proposal: Proposal }) {
  const d = proposal.decision;
  if (!d) {
    return (
      <section aria-label="Policy verdict">
        <h4>Policy</h4>
        <p className="hint">No policy decision is recorded.</p>
      </section>
    );
  }
  const tone = VERDICT_TONE[d.verdict as keyof typeof VERDICT_TONE] ?? "info";
  return (
    <section aria-label="Policy verdict">
      <h4>Policy</h4>
      <p>
        <span className={`badge ${tone}`} data-testid="verdict">{d.verdict}</span>
        {d.rule_ids.length > 0 ? (
          <span className="rule-ids" data-testid="rule-ids">
            {" "}
            by {d.rule_ids.join(", ")}
          </span>
        ) : null}
      </p>
      {d.reasons.length > 0 ? (
        <ul className="reasons">
          {d.reasons.map((r, i) => (
            <li key={`${i}-${r}`}>{r}</li>
          ))}
        </ul>
      ) : (
        <p className="hint">No rule objected.</p>
      )}
    </section>
  );
}
