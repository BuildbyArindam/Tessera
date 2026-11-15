"use client";

import { useState } from "react";
import { AutonomyBadge } from "@/components/AutonomyBadge";
import { BusinessPicker } from "@/components/Pickers";
import { ProposalList } from "@/components/ProposalList";
import { Async, EmptyState, Skeleton } from "@/components/States";
import { loadBusinesses, loadForecasts } from "@/lib/queries";
import { useApi } from "@/lib/useApi";
import { useProposals } from "@/lib/useProposals";

export default function ProposalsPage() {
  const businesses = useApi(loadBusinesses, []);
  const [chosen, setChosen] = useState<string | null>(null);

  return (
    <>
      <div className="page-head">
        <h1>Proposals</h1>
        <AutonomyBadge />
      </div>
      <Async
        remote={businesses}
        isEmpty={(d) => d.length === 0}
        empty={<EmptyState title="No businesses yet">Run <code>python scripts/seed_demo.py</code> to load the demo business.</EmptyState>}
        skeleton={<Skeleton height={48} label="Loading businesses" />}
      >
        {(list) => {
          const businessId = chosen && list.some((b) => b.id === chosen) ? chosen : (list[0]?.id ?? "");
          return (
            <>
              <div className="controls">
                <BusinessPicker businesses={list} value={businessId} onChange={setChosen} />
              </div>
              <ProposalsFor key={businessId} businessId={businessId} />
            </>
          );
        }}
      </Async>
    </>
  );
}

function ProposalsFor({ businessId }: { businessId: string }) {
  const { state, applyUpdate } = useProposals(businessId);
  const forecasts = useApi(() => loadForecasts(businessId), [businessId]);

  return (
    <section className="section" aria-label="Proposals">
      <Async
        remote={state}
        isEmpty={(d) => d.length === 0}
        empty={<EmptyState title="No proposals yet">Run a plan to have the agent propose treasury actions.</EmptyState>}
        skeleton={<Skeleton height={220} label="Loading proposals" />}
      >
        {(proposals) => (
          <ProposalList proposals={proposals} forecasts={forecasts} onChanged={applyUpdate} />
        )}
      </Async>
    </section>
  );
}
