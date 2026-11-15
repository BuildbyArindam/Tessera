"use client";

import { loadAutonomy } from "@/lib/queries";
import { useApi } from "@/lib/useApi";
import { formatDate } from "@/lib/format";
import type { Autonomy } from "@/api/client";
import { Skeleton } from "./States";

export const LEVEL_NAME: Record<Autonomy["level"], string> = {
  L0: "Advise only",
  L1: "Low-risk actions",
  L2: "Standard actions",
  L3: "Extended actions",
};

export function AutonomyBadge() {
  const remote = useApi(loadAutonomy, []);
  if (remote.status === "loading") return <Skeleton height={64} label="Loading autonomy level" />;
  if (remote.status === "error") {
    return (
      <div className="autonomy" role="alert">
        <span className="level">Autonomy level unavailable</span>
        <button type="button" onClick={remote.retry}>
          Try again
        </button>
      </div>
    );
  }
  const { level, since, pending_promotion } = remote.data;
  return (
    <div className="autonomy" role="group" aria-label={`Current autonomy level ${level}, ${LEVEL_NAME[level]}`}>
      <span className="level">
        Autonomy {level} · {LEVEL_NAME[level]}
      </span>
      <span className="sub">
        Since {formatDate(since)}
        {pending_promotion ? " · promotion awaiting human confirmation" : ""}
      </span>
    </div>
  );
}
