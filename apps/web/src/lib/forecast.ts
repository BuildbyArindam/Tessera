import type { Currency, Forecast } from "@/api/client";
import { toNumber } from "./format";

export const WINDOWS = [30, 60, 90] as const;
export type Window = (typeof WINDOWS)[number];
/** 30 / 60 / 90 days are 4 / 8 / 13 weeks of weekly forecasts (28, 56, 91 days). */
export const WINDOW_WEEKS: Record<Window, number> = { 30: 4, 60: 8, 90: 13 };

/** x-axis labels for the weeks that match the 30 / 60 / 90 day windows. */
export const WEEK_LABEL: Record<number, string> = { 4: "30 d", 8: "60 d", 13: "90 d" };

export interface ForecastRow {
  week: number;
  days: number;
  point: number;
  lower: number;
  upper: number;
  /** [lower, upper] for a range area: it shades the interval correctly even when lower is negative. */
  range: [number, number];
}

export interface CurrencyForecast {
  currency: Currency;
  asOf: string;
  coverage: number;
  rows: ForecastRow[];
}

/** Group the newest forecasts by currency and cut each series at the chosen window. Sorted by currency and week. */
export function groupForecasts(forecasts: Forecast[], window: Window): CurrencyForecast[] {
  const maxWeek = WINDOW_WEEKS[window];
  const by = new Map<Currency, Forecast[]>();
  for (const f of forecasts) {
    if (f.horizon_weeks > maxWeek) continue;
    by.set(f.currency, [...(by.get(f.currency) ?? []), f]);
  }
  return [...by.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([currency, list]) => {
      const sorted = [...list].sort((a, b) => a.horizon_weeks - b.horizon_weeks);
      return {
        currency,
        asOf: sorted[0]?.as_of ?? "",
        coverage: sorted[0]?.target_coverage ?? 0.8,
        rows: sorted.map((f) => {
          const lower = toNumber(f.lower.amount);
          const upper = toNumber(f.upper.amount);
          return { week: f.horizon_weeks, days: f.horizon_weeks * 7, point: toNumber(f.point.amount), lower, upper, range: [lower, upper] };
        }),
      };
    });
}
