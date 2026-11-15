"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/api/client";

export type Remote<T> =
  | { status: "loading" }
  | { status: "error"; message: string; retry: () => void }
  | { status: "ready"; data: T };

/** Run `load` whenever `deps` change. A stale response from an earlier call can never overwrite a newer one. */
export function useApi<T>(load: () => Promise<T>, deps: readonly unknown[]): Remote<T> {
  const [state, setState] = useState<Remote<T>>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const latest = useRef(0);
  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  useEffect(() => {
    const id = ++latest.current;
    setState({ status: "loading" });
    load().then(
      (data) => {
        if (id === latest.current) setState({ status: "ready", data });
      },
      (err: unknown) => {
        if (id !== latest.current) return;
        const message = err instanceof ApiError ? err.message : "Could not reach the API.";
        setState({ status: "error", message, retry });
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, attempt]);

  return state;
}
