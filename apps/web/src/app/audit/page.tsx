"use client";

import { useState } from "react";
import type { AuditEvent } from "@/api/client";
import { AuditDetail } from "@/components/AuditDetail";
import { AuditList } from "@/components/AuditList";
import { Async, EmptyState, Skeleton } from "@/components/States";
import { VerifyChain } from "@/components/VerifyChain";
import { AUDIT_PAGE_SIZE, loadAuditPage } from "@/lib/queries";
import { useApi } from "@/lib/useApi";

export default function AuditPage() {
  // Pages are read with the API's cursor: `after_seq`. `cursors` is the trail of cursors, so Previous is just a pop.
  const [cursors, setCursors] = useState<number[]>([0]);
  const [selected, setSelected] = useState<AuditEvent | null>(null);
  const [brokenSeq, setBrokenSeq] = useState<number | null>(null);
  const cursor = cursors[cursors.length - 1] ?? 0;
  const remote = useApi(() => loadAuditPage(cursor), [cursor]);

  const go = (next: number[]) => {
    setSelected(null);
    setCursors(next);
  };

  return (
    <>
      <div className="page-head">
        <h1>Audit</h1>
        <VerifyChainWithMemory onBroken={setBrokenSeq} onShow={(seq) => go([Math.max(seq - 1, 0)])} />
      </div>
      <p className="hint">Every forecast, proposal, policy decision, approval and execution, in order. Oldest first.</p>
      <Async
        remote={remote}
        isEmpty={(d) => d.events.length === 0 && cursors.length <= 1}
        empty={<EmptyState title="No audit events yet">Events appear here as soon as the system does something.</EmptyState>}
        skeleton={<Skeleton height={320} label="Loading audit events" />}
      >
        {(page) => (
          <>
            {page.events.length > 0 ? (
              <AuditList events={page.events} selectedSeq={selected?.seq ?? null} brokenSeq={brokenSeq} onSelect={setSelected} />
            ) : (
              // The previous page was exactly full, so the API could not know it was the last one.
              <p className="hint" data-testid="no-more-events">No more events. The previous page was the last.</p>
            )}
            <nav className="pager" aria-label="Audit pages">
              <button type="button" disabled={cursors.length <= 1} onClick={() => go(cursors.slice(0, -1))}>
                Previous
              </button>
              <span className="hint">
                {page.events.length > 0
                  ? `Events ${page.events[0]?.seq}–${page.events[page.events.length - 1]?.seq} · ${AUDIT_PAGE_SIZE} per page`
                  : `${AUDIT_PAGE_SIZE} per page`}
              </span>
              <button type="button" disabled={page.next_after_seq === null} onClick={() => go([...cursors, page.next_after_seq as number])}>
                Next
              </button>
            </nav>
          </>
        )}
      </Async>
      {selected ? <AuditDetail event={selected} onClose={() => setSelected(null)} /> : null}
    </>
  );
}

/** Verify Chain, plus remembering which row (if any) the last check found broken so the list can mark it. */
function VerifyChainWithMemory({ onBroken, onShow }: { onBroken: (seq: number | null) => void; onShow: (seq: number) => void }) {
  return (
    <VerifyChain
      onShowBroken={(seq) => {
        onBroken(seq);
        onShow(seq);
      }}
    />
  );
}
