"use client";

import { useState } from "react";
import { ApiError, type ChainStatus } from "@/api/client";
import { verifyAuditChain } from "@/lib/queries";

/** Asks the API to re-walk the whole chain. Shows OK, or the first broken sequence number and why. */
export function VerifyChain({ onShowBroken }: { onShowBroken: (seq: number) => void }) {
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<ChainStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function verify() {
    setBusy(true);
    setError(null);
    try {
      setStatus(await verifyAuditChain());
    } catch (err) {
      setStatus(null);
      setError(err instanceof ApiError ? err.message : "Could not reach the API, so the chain was not checked.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="verify">
      <button type="button" disabled={busy} aria-busy={busy} onClick={() => void verify()}>
        {busy ? "Verifying…" : "Verify chain"}
      </button>
      {status?.ok ? (
        <p role="status" className="badge low" data-testid="chain-ok">
          ✓ Chain OK · {status.checked} events checked{status.head_seq !== null ? ` · head #${status.head_seq}` : ""}
        </p>
      ) : null}
      {status && !status.ok ? (
        <div role="alert" className="state error" data-testid="chain-broken">
          <p>
            ✗ Chain broken at event #{status.broken_seq ?? "?"}
            {status.reason ? `: ${status.reason}` : ""}
          </p>
          {status.broken_seq !== null ? (
            <button type="button" onClick={() => onShowBroken(status.broken_seq as number)}>
              Show event #{status.broken_seq}
            </button>
          ) : null}
        </div>
      ) : null}
      {error ? (
        <p role="alert" className="state error">
          {error}
        </p>
      ) : null}
    </div>
  );
}
