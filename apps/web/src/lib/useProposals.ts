"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type Proposal } from "@/api/client";
import { loadProposals } from "./queries";
import { IN_FLIGHT, replaceProposal } from "./proposals";
import type { Remote } from "./useApi";

export const POLL_MS = 4000;

/**
 * The proposals for one business, always as the API last reported them.
 * While any proposal is still moving (approved or submitted) the list refreshes itself, so a status only ever
 * advances when the worker has actually advanced it. The newest request wins: a slow, older response is dropped.
 */
export function useProposals(businessId: string) {
  const [state, setState] = useState<Remote<Proposal[]>>({ status: "loading" });
  const latest = useRef(0);

  const refresh = useCallback(
    async (opts: { quiet?: boolean } = {}) => {
      const id = ++latest.current;
      if (!opts.quiet) setState({ status: "loading" });
      try {
        const data = await loadProposals(businessId);
        if (id === latest.current) setState({ status: "ready", data });
      } catch (err) {
        if (id !== latest.current) return;
        // A failed background refresh keeps what is on screen; only a visible load turns into an error state.
        if (opts.quiet) return;
        const message = err instanceof ApiError ? err.message : "Could not reach the API.";
        setState({ status: "error", message, retry: () => void refresh() });
      }
    },
    [businessId],
  );

  useEffect(() => {
    const requests = latest; // a request counter, not a DOM node: the cleanup must see its value at cleanup time
    void refresh();
    return () => {
      requests.current++; // drop anything still in flight when the business changes or the page closes
    };
  }, [refresh]);

  const moving = state.status === "ready" && state.data.some((p) => IN_FLIGHT.includes(p.status));
  useEffect(() => {
    if (!moving) return;
    const timer = setInterval(() => void refresh({ quiet: true }), POLL_MS);
    return () => clearInterval(timer);
  }, [moving, refresh]);

  /** Take the API's answer to a write as the new truth for that proposal, then re-read the whole list to confirm. */
  const applyUpdate = useCallback(
    (updated: Proposal) => {
      latest.current++; // any request already in flight was sent before this answer and must not overwrite it
      setState((s) => (s.status === "ready" ? { status: "ready", data: replaceProposal(s.data, updated) } : s));
      void refresh({ quiet: true });
    },
    [refresh],
  );

  return { state, refresh, applyUpdate };
}
