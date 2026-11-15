import { api, unwrap } from "@/api/client";

/**
 * Every write returns what the API now says, and callers render THAT. Nothing here (or in a caller) assumes the
 * outcome: an approval is only shown as approved, and nothing as executed, once the API's response says so.
 */
export async function decideProposal(id: string, decision: "approve" | "reject") {
  const path = decision === "approve" ? "/proposals/{proposal_id}/approve" : "/proposals/{proposal_id}/reject";
  return unwrap(await api.POST(path, { params: { path: { proposal_id: id } }, body: {} }));
}

export async function confirmPromotion(promotionId: number) {
  return unwrap(await api.POST("/autonomy", { body: { action: "confirm_promotion", promotion_id: promotionId } }));
}
