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

export const loadProposals = async (businessId: string) =>
  unwrap(await api.GET("/proposals", { params: { query: { business_id: businessId, limit: 200 } } }));

export const loadPolicies = async () => unwrap(await api.GET("/policies"));

export const AUDIT_PAGE_SIZE = 20;
export const loadAuditPage = async (afterSeq: number) =>
  unwrap(await api.GET("/audit", { params: { query: { after_seq: afterSeq, limit: AUDIT_PAGE_SIZE } } }));

export const verifyAuditChain = async () => unwrap(await api.GET("/audit/verify"));
