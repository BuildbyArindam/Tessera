"use client";

import { loadForecasts } from "@/lib/queries";
import { useApi } from "@/lib/useApi";
import { groupForecasts, type Window } from "@/lib/forecast";
import { Async, EmptyState, Skeleton } from "./States";
import { ForecastChart } from "./ForecastChart";

export function ForecastSection({ businessId, window }: { businessId: string; window: Window }) {
  const remote = useApi(() => loadForecasts(businessId), [businessId]);
  return (
    <section className="section" aria-labelledby="forecast-h">
      <h2 id="forecast-h">Forecast · next {window} days</h2>
      <p className="hint">Each point is the net cash flow (inflows minus outflows) expected in that single week, not a running balance.</p>
      <div style={{ height: 12 }} />
      <Async
        remote={remote}
        isEmpty={(d) => groupForecasts(d, window).length === 0}
        empty={<EmptyState title="No forecast yet">Run the seed script or the worker to create one.</EmptyState>}
        skeleton={<Skeleton height={300} label="Loading forecasts" />}
      >
        {(d) => (
          <div className="grid wide">
            {groupForecasts(d, window).map((g) => (
              <ForecastChart key={g.currency} data={g} />
            ))}
          </div>
        )}
      </Async>
    </section>
  );
}
