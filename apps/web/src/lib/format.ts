import type { Money } from "@/api/client";

/** Money arrives as a decimal STRING. Intl formats it without converting to a binary float first. */
export function formatMoney(m: Money, opts: { compact?: boolean; sign?: boolean } = {}): string {
  const fmt = new Intl.NumberFormat("en", {
    style: "currency",
    currency: m.currency,
    notation: opts.compact ? "compact" : "standard",
    maximumFractionDigits: opts.compact ? 1 : 2,
    signDisplay: opts.sign ? "exceptZero" : "auto",
  });
  return fmt.format(m.amount as unknown as number);
}

export function formatDate(iso: string): string {
  return new Intl.DateTimeFormat("en", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }).format(
    new Date(`${iso.slice(0, 10)}T00:00:00Z`),
  );
}

export function formatDateTime(iso: string): string {
  const fmt = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "medium", timeZone: "UTC" });
  return `${fmt.format(new Date(iso))} UTC`;
}

export function formatPercent(p: number): string {
  return `${Math.round(p * 100)}%`;
}

export function describeDue(days: number): string {
  if (days < 0) return `${-days} day${days === -1 ? "" : "s"} overdue`;
  if (days === 0) return "due today";
  return `in ${days} day${days === 1 ? "" : "s"}`;
}

/** For chart axes only: a display number, never used for arithmetic on money. */
export const toNumber = (amount: string): number => Number(amount);
