export type RiskLevel = "late" | "high" | "medium" | "low";

export const HIGH_RISK = 0.6;
export const MEDIUM_RISK = 0.3;

/** `p_late` is the probability of payment more than 7 days after the due date. */
export function riskLevel(pLate: number, alreadyLate: boolean): RiskLevel {
  if (alreadyLate) return "late";
  if (pLate >= HIGH_RISK) return "high";
  if (pLate >= MEDIUM_RISK) return "medium";
  return "low";
}

export const RISK_LABEL: Record<RiskLevel, string> = {
  late: "Already late",
  high: "High risk",
  medium: "Medium risk",
  low: "Low risk",
};
