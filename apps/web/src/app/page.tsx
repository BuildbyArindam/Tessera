"use client";

import { useState } from "react";
import { AutonomyBadge } from "@/components/AutonomyBadge";
import { BalanceCards } from "@/components/BalanceCards";
import { ForecastSection } from "@/components/ForecastSection";
import { InvoiceRisk } from "@/components/InvoiceRisk";
import { Obligations } from "@/components/Obligations";
import { BusinessPicker, WindowPicker } from "@/components/Pickers";
import { Async, EmptyState, Skeleton } from "@/components/States";
import type { Window } from "@/lib/forecast";
import { loadBusinesses } from "@/lib/queries";
import { useApi } from "@/lib/useApi";

export default function OverviewPage() {
  const remote = useApi(loadBusinesses, []);
  const [chosen, setChosen] = useState<string | null>(null);
  const [window, setWindow] = useState<Window>(90);

  return (
    <>
      <div className="page-head">
        <h1>Overview</h1>
        <AutonomyBadge />
      </div>
      <Async
        remote={remote}
        isEmpty={(d) => d.length === 0}
        empty={<EmptyState title="No businesses yet">Run <code>python scripts/seed_demo.py</code> to load the demo business.</EmptyState>}
        skeleton={<Skeleton height={48} label="Loading businesses" />}
      >
        {(businesses) => {
          const businessId = chosen && businesses.some((b) => b.id === chosen) ? chosen : (businesses[0]?.id ?? "");
          return (
            <>
              <div className="controls">
                <BusinessPicker businesses={businesses} value={businessId} onChange={setChosen} />
                <WindowPicker value={window} onChange={setWindow} />
              </div>
              <BalanceCards businessId={businessId} />
              <ForecastSection businessId={businessId} window={window} />
              <Obligations businessId={businessId} days={window} />
              <InvoiceRisk businessId={businessId} />
            </>
          );
        }}
      </Async>
    </>
  );
}
