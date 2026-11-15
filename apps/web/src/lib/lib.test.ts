import { describe, expect, it } from "vitest";
import type { Forecast } from "@/api/client";
import { describeDue, formatMoney, formatPercent } from "./format";
import { groupForecasts } from "./forecast";
import { HIGH_RISK, MEDIUM_RISK, riskLevel } from "./risk";

const money = (amount: string, currency: "USD" | "EUR" = "USD") => ({ amount, currency });
const fc = (currency: "USD" | "EUR", week: number, point: string, lower: string, upper: string): Forecast => ({
  id: week,
  business_id: "B01",
  currency,
  horizon_weeks: week,
  as_of: "2026-06-30",
  point: money(point, currency),
  lower: money(lower, currency),
  upper: money(upper, currency),
  target_coverage: 0.8,
  model_version: "v1",
});

describe("formatMoney", () => {
  it("formats a decimal string without a float round trip", () => {
    expect(formatMoney(money("1234.50"))).toBe("$1,234.50");
    expect(formatMoney(money("12345678901234.56"))).toBe("$12,345,678,901,234.56");
  });
  it("shows a sign on request and compacts large values", () => {
    expect(formatMoney(money("-5.00"), { sign: true })).toContain("-");
    expect(formatMoney(money("2500000"), { compact: true })).toBe("$2.5M");
  });
});

describe("describeDue", () => {
  it("reads naturally either side of today", () => {
    expect(describeDue(-1)).toBe("1 day overdue");
    expect(describeDue(-10)).toBe("10 days overdue");
    expect(describeDue(0)).toBe("due today");
    expect(describeDue(1)).toBe("in 1 day");
    expect(describeDue(7)).toBe("in 7 days");
  });
  it("rounds a probability to a whole percent", () => expect(formatPercent(0.4249)).toBe("42%"));
});

describe("riskLevel", () => {
  it("puts an already-late invoice first regardless of p_late", () => expect(riskLevel(0.01, true)).toBe("late"));
  it("splits at the documented thresholds", () => {
    expect(riskLevel(HIGH_RISK, false)).toBe("high");
    expect(riskLevel(HIGH_RISK - 0.001, false)).toBe("medium");
    expect(riskLevel(MEDIUM_RISK, false)).toBe("medium");
    expect(riskLevel(MEDIUM_RISK - 0.001, false)).toBe("low");
  });
});

describe("groupForecasts", () => {
  const all = [
    fc("USD", 13, "1", "-5", "9"),
    fc("EUR", 4, "2", "1", "3"),
    fc("USD", 4, "-3", "-10", "4"),
    fc("USD", 8, "0", "-6", "6"),
  ];
  it("groups by currency, sorts by week and keeps the interval as [lower, upper] even when negative", () => {
    const [eur, usd] = groupForecasts(all, 90);
    expect(eur?.currency).toBe("EUR");
    expect(usd?.rows.map((r) => r.week)).toEqual([4, 8, 13]);
    expect(usd?.rows[0]?.range).toEqual([-10, 4]);
    expect(usd?.rows[0]?.days).toBe(28);
  });
  it("cuts the series at the chosen window", () => {
    expect(groupForecasts(all, 30).find((g) => g.currency === "USD")?.rows.map((r) => r.week)).toEqual([4]);
    expect(groupForecasts(all, 60).find((g) => g.currency === "USD")?.rows.map((r) => r.week)).toEqual([4, 8]);
  });
  it("returns nothing for nothing", () => expect(groupForecasts([], 90)).toEqual([]));
});
