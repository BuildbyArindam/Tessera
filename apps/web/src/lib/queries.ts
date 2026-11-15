import { api, unwrap } from "@/api/client";

/** One function per screen section. Each throws an ApiError the section turns into its error state. */
export const loadBusinesses = async () => unwrap(await api.GET("/businesses"));

export const loadBalances = async (businessId: string) =>
  unwrap(await api.GET("/businesses/{business_id}/balances", { params: { path: { business_id: businessId } } }));

export const loadForecasts = async (businessId: string) =>
  unwrap(await api.GET("/forecasts", { params: { query: { business_id: businessId, latest: true } } }));

export const loadObligations = async (businessId: string, daysAhead: number) =>
  unwrap(
    await api.GET("/businesses/{business_id}/obligations", {
      params: { path: { business_id: businessId }, query: { days_ahead: daysAhead, limit: 12 } },
    }),
  );

export const loadRiskyInvoices = async (businessId: string) =>
  unwrap(
    await api.GET("/invoices", {
      params: { query: { business_id: businessId, status: "OPEN", order_by: "risk", limit: 10 } },
    }),
  );

export const loadAutonomy = async () => unwrap(await api.GET("/autonomy"));
