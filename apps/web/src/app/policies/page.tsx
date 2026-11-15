"use client";

import { AutonomySection } from "@/components/AutonomySection";
import { RuleCard } from "@/components/RuleCard";
import { Async, EmptyState } from "@/components/States";
import { loadPolicies } from "@/lib/queries";
import { useApi } from "@/lib/useApi";

export default function PoliciesPage() {
  const remote = useApi(loadPolicies, []);
  return (
    <>
      <div className="page-head">
        <h1>Policies</h1>
      </div>
      <p className="hint">
        Every proposal is checked against these rules before anything executes. They are read-only here: change one by editing its YAML file.
      </p>
      <Async
        remote={remote}
        isEmpty={(d) => d.rules.length === 0}
        empty={<EmptyState title="No rules are loaded">The API refuses to start without a ruleset, so this should not happen.</EmptyState>}
      >
        {(policy) => (
          <>
            <section className="section" aria-labelledby="hash-h">
              <h2 id="hash-h">Ruleset in force</h2>
              <div className="card">
                <p className="hint">Ruleset hash. Every decision records the hash of the rules that made it.</p>
                <p className="mono hash" data-testid="ruleset-hash">{policy.ruleset_hash}</p>
                <p className="hint">{policy.rules.length} rules</p>
              </div>
            </section>
            <section className="section" aria-labelledby="rules-h">
              <h2 id="rules-h">Rules</h2>
              <div className="grid wide">
                {policy.rules.map((r) => (
                  <RuleCard key={r.id} rule={r} />
                ))}
              </div>
            </section>
          </>
        )}
      </Async>
      <AutonomySection />
    </>
  );
}
