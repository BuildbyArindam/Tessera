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
  throw new ApiError(`The API returned an unexpected response${status ? ` (${status})` : ""}.`, status);
}
