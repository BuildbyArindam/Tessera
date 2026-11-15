import createClient from "openapi-fetch";
import type { components, paths } from "./schema";

/** Typed client generated from the FastAPI OpenAPI schema (`npm run gen:api`). The browser calls our own proxy. */
export const api = createClient<paths>({ baseUrl: "/api/tessera" });

export type Schemas = components["schemas"];
export type Currency = Schemas["Currency"];
export type Money = Schemas["Money-Output"];
export type Forecast = Schemas["ForecastOut"];
export type Balance = Schemas["BalanceOut"];
export type Obligation = Schemas["ObligationOut"];
export type InvoiceRisk = Schemas["InvoiceRiskOut"];
export type Autonomy = Schemas["AutonomyOut"];
export type Business = Schemas["BusinessOut"];
export type Proposal = Schemas["ProposalOut"];
export type Decision = Schemas["DecisionOut"];
export type Simulation = Schemas["SimulationOut"];
export type Policy = Schemas["PolicyOut"];
export type Rule = Schemas["RuleOut"];
export type AuditEvent = Schemas["AuditRecordOut"];
export type AuditPage = Schemas["AuditPage"];
export type ChainStatus = Schemas["ChainStatusOut"];

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Turn an openapi-fetch result into its data, or throw an ApiError whose message is safe to show. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.data !== undefined) return result.data;
  const status = result.response?.status ?? null;
  if (status === 401) throw new ApiError("The dashboard is not authorised to read the API (check TESSERA_API_KEY).", 401);
  if (status === 503) throw new ApiError("The database is unavailable.", 503);
  if (status === 404) throw new ApiError("Not found.", 404);
  // A conflict is the API explaining that the state moved on (already decided, evidence no longer enough). Its
  // `detail` is server-authored text, not model or invoice text, so it is safe to show.
  if (status === 409) throw new ApiError(detailOf(result.error) ?? "That conflicts with the current state. Refresh and look again.", 409);
  if (status === 405 || status === 403) throw new ApiError("The dashboard is not allowed to do that.", status);
  throw new ApiError(`The API returned an unexpected response${status ? ` (${status})` : ""}.`, status);
}

function detailOf(error: unknown): string | null {
  const detail = (error as { detail?: unknown } | null | undefined)?.detail;
  return typeof detail === "string" && detail.length <= 300 ? detail : null;
}
