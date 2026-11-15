"use client";

import { Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Money } from "@/api/client";
import { formatDate, formatMoney, formatPercent } from "@/lib/format";
import { WEEK_LABEL, type CurrencyForecast, type ForecastRow } from "@/lib/forecast";

const money = (currency: CurrencyForecast["currency"], amount: number, compact = false) =>
  formatMoney({ amount: String(amount), currency } as Money, { compact });

export function ForecastChart({ data }: { data: CurrencyForecast }) {
  const { currency, rows, coverage, asOf } = data;
  const last = rows[rows.length - 1];
  const tickDays = rows.filter((r) => r.week in WEEK_LABEL).map((r) => r.days);
  const summary = last
    ? `Weekly net cash flow forecast in ${currency}, ${rows.length} weeks from ${formatDate(asOf)}. Last week: ${money(currency, last.point)}, ${formatPercent(coverage)} interval ${money(currency, last.lower)} to ${money(currency, last.upper)}.`
    : "";

  return (
    <figure className="card" style={{ margin: 0 }}>
      <figcaption>
        <h3>{currency} · expected net cash flow per week</h3>
        <p className="hint">
          Line: point forecast. Shaded band: {formatPercent(coverage)} interval. Made {formatDate(asOf)}.
        </p>
      </figcaption>
      <div role="img" aria-label={summary} style={{ height: 240, marginTop: 8 }}>
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={rows} margin={{ top: 8, right: 12, bottom: 4, left: 4 }} accessibilityLayer={false}>
            <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" />
            <XAxis
              dataKey="days"
              type="number"
              domain={[7, "dataMax"]}
              ticks={tickDays}
              tickFormatter={(d: number) => WEEK_LABEL[d / 7] ?? ""}
              stroke="var(--muted)"
            />
            <YAxis width={84} tickFormatter={(v: number) => money(currency, v, true)} stroke="var(--muted)" />
            <Tooltip content={<ChartTooltip currency={currency} coverage={coverage} />} />
            <Area dataKey="range" stroke="none" fill="var(--accent)" fillOpacity={0.2} isAnimationActive={false} />
            <Line dataKey="point" stroke="var(--accent)" strokeWidth={2.5} dot={false} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <details>
        <summary>View {currency} forecast as a table</summary>
        <table>
          <caption className="sr-only">{currency} weekly net cash flow forecast</caption>
          <thead>
            <tr>
              <th scope="col">Week</th>
              <th scope="col" className="r">Point</th>
              <th scope="col" className="r">Low</th>
              <th scope="col" className="r">High</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.week}>
                <th scope="row">{r.week}</th>
                <td className="r">{money(currency, r.point)}</td>
                <td className="r">{money(currency, r.lower)}</td>
                <td className="r">{money(currency, r.upper)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}

function ChartTooltip({
  active,
  payload,
  currency,
  coverage,
}: {
  active?: boolean;
  payload?: { payload: ForecastRow }[];
  currency: CurrencyForecast["currency"];
  coverage: number;
}) {
  const row = payload?.[0]?.payload;
  if (!active || !row) return null;
  return (
    <div className="card" style={{ padding: 10 }}>
      <strong>Week {row.week} (day {row.days})</strong>
      <div>Point: {money(currency, row.point)}</div>
      <div>
        {formatPercent(coverage)} interval: {money(currency, row.lower)} to {money(currency, row.upper)}
      </div>
    </div>
  );
}
