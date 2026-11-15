"use client";

import { useId, useRef, useState, type KeyboardEvent } from "react";
import type { Forecast, Proposal } from "@/api/client";
import { ACTION_LABEL, STATUS_LABEL, TABS, tabOf, type Tab } from "@/lib/proposals";
import type { Remote } from "@/lib/useApi";
import { ProposalCard } from "./ProposalCard";
import { EmptyState } from "./States";

const EMPTY: Record<Tab, { title: string; body: string }> = {
  pending: { title: "Nothing needs your approval", body: "Escalated proposals appear here for a one-tap decision." },
  decided: { title: "No decided proposals yet", body: "Approved, rejected and executing proposals appear here." },
  blocked: { title: "Nothing has been blocked", body: "Proposals the policy engine refuses appear here, with the rule that refused them." },
};

export function ProposalList({
  proposals,
  forecasts,
  onChanged,
}: {
  proposals: Proposal[];
  forecasts: Remote<Forecast[]>;
  onChanged: (updated: Proposal) => void;
}) {
  const [active, setActive] = useState<Tab>("pending");
  const [notice, setNotice] = useState("");
  const base = useId();
  const refs = useRef<Record<Tab, HTMLButtonElement | null>>({ pending: null, decided: null, blocked: null });
  const counts: Record<Tab, number> = { pending: 0, decided: 0, blocked: 0 };
  for (const p of proposals) counts[tabOf(p.status)]++;
  const shown = proposals.filter((p) => tabOf(p.status) === active);

  /** After a decision, say what the API reported and where the proposal went (it leaves this tab), in a live region. */
  function changed(updated: Proposal) {
    const where = TABS.find((t) => t.id === tabOf(updated.status))?.label ?? "";
    setNotice(`${ACTION_LABEL[updated.action_type] ?? updated.action_type} ${updated.id}: ${STATUS_LABEL[updated.status]}. See it under "${where}".`);
    onChanged(updated);
  }

  function onKeyDown(e: KeyboardEvent, index: number) {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    const jump = e.key === "Home" ? 0 : e.key === "End" ? TABS.length - 1 : null;
    if (step === 0 && jump === null) return;
    e.preventDefault();
    const target = jump ?? (index + step + TABS.length) % TABS.length;
    const next = TABS[target]!.id;
    setActive(next);
    refs.current[next]?.focus();
  }

  return (
    <div>
      <div role="tablist" aria-label="Proposals by state" className="tabs">
        {TABS.map((t, i) => (
          <button
            key={t.id}
            ref={(el) => { refs.current[t.id] = el; }}
            id={`${base}-tab-${t.id}`}
            type="button"
            role="tab"
            aria-selected={active === t.id}
            aria-controls={`${base}-panel`}
            tabIndex={active === t.id ? 0 : -1}
            onClick={() => setActive(t.id)}
            onKeyDown={(e) => onKeyDown(e, i)}
          >
            {t.label} <span className="count">({counts[t.id]})</span>
          </button>
        ))}
      </div>
      <p role="status" className="notice" data-testid="notice">
        {notice}
      </p>
      <div role="tabpanel" id={`${base}-panel`} aria-labelledby={`${base}-tab-${active}`} className="stack">
        {shown.length === 0 ? (
          <EmptyState title={EMPTY[active].title}>{EMPTY[active].body}</EmptyState>
        ) : (
          shown.map((p) => <ProposalCard key={p.id} proposal={p} forecasts={forecasts} onChanged={changed} />)
        )}
      </div>
    </div>
  );
}
