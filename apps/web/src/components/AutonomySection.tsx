"use client";

import { useState } from "react";
import { ApiError, type Autonomy } from "@/api/client";
import { formatDateTime } from "@/lib/format";
import { confirmPromotion } from "@/lib/mutations";
import { loadAutonomy } from "@/lib/queries";
import { useApi } from "@/lib/useApi";
import { LEVEL_NAME } from "./AutonomyBadge";
import { Async } from "./States";

export function AutonomySection() {
  const remote = useApi(loadAutonomy, []);
  const [current, setCurrent] = useState<Autonomy | null>(null); // the API's latest answer to a confirmation
  return (
    <section className="section" aria-labelledby="autonomy-h">
      <h2 id="autonomy-h">Autonomy</h2>
      <Async remote={remote} isEmpty={() => false} empty={null}>
        {(loaded) => <AutonomyBody autonomy={current ?? loaded} onUpdated={setCurrent} />}
      </Async>
    </section>
  );
}

function AutonomyBody({ autonomy, onUpdated }: { autonomy: Autonomy; onUpdated: (a: Autonomy) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const { level, since, pending_promotion: pending, assessment, evidence_available: haveEvidence } = autonomy;
  const met = assessment?.criteria.filter((c) => c.passed).length ?? 0;
  const total = assessment?.criteria.length ?? 0;

  async function confirm(id: number) {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const next = await confirmPromotion(id);
      onUpdated(next);
      // Said only after the API answered, and in terms of the level it reports.
      setResult(next.level === level ? `The API kept the level at ${next.level}.` : `The API reports the level is now ${next.level}.`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the API. Nothing was changed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <p>
        <strong data-testid="autonomy-level">
          {level} · {LEVEL_NAME[level]}
        </strong>
        <span className="hint"> since {formatDateTime(since)}</span>
      </p>

      {!haveEvidence ? (
        <p className="hint" data-testid="no-evidence">
          No evidence source is configured, so nothing can be promoted or confirmed yet.
        </p>
      ) : assessment ? (
        <>
          <h3>Evidence toward promotion</h3>
          <p>
            <progress value={met} max={Math.max(total, 1)} aria-label="Promotion criteria met" /> {met} of {total} criteria met
          </p>
          <table>
            <caption className="sr-only">Promotion criteria</caption>
            <thead>
              <tr>
                <th scope="col">Criterion</th>
                <th scope="col">Observed</th>
                <th scope="col">Required</th>
                <th scope="col">Result</th>
              </tr>
            </thead>
            <tbody>
              {assessment.criteria.map((c) => (
                <tr key={c.name}>
                  <th scope="row">{c.name}</th>
                  <td>{c.observed}</td>
                  <td>{c.required}</td>
                  <td>
                    <span className={`badge ${c.passed ? "low" : "medium"}`}>{c.passed ? "✓ Met" : "✗ Not met"}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {assessment.triggers.length > 0 ? <p className="hint">Triggers: {assessment.triggers.join(", ")}</p> : null}
          <p className="hint mono">thresholds {assessment.config_hash}</p>
        </>
      ) : null}

      {pending && pending.status === "PENDING" ? (
        <div className="promotion">
          <p>
            Promotion {pending.from_level} → {pending.to_level} was proposed {formatDateTime(pending.proposed_at)} and is waiting for a human to
            confirm it. Confirming re-checks the evidence first.
          </p>
          <button type="button" className="primary" disabled={busy || !haveEvidence} aria-busy={busy} onClick={() => void confirm(pending.id)}>
            {busy ? "Confirming…" : "Confirm promotion"}
          </button>
          {!haveEvidence ? <p className="hint">Unavailable until an evidence source is configured.</p> : null}
        </div>
      ) : (
        <p className="hint">No promotion is waiting for confirmation.</p>
      )}
      {result ? <p role="status">{result}</p> : null}
      {error ? (
        <p className="state error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
