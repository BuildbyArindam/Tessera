import type { Rule } from "@/api/client";
import { ParamTree } from "./ParamTree";

export function RuleCard({ rule }: { rule: Rule }) {
  const empty = Object.keys(rule.params).length === 0;
  const large = JSON.stringify(rule.params).length > 400;
  return (
    <article className="card rule" data-rule-id={rule.id}>
      <h3>{rule.id}</h3>
      <p className="hint mono">
        {rule.type} · version {rule.version}
      </p>
      {empty ? (
        <p className="hint">No parameters. Its state is set at runtime.</p>
      ) : (
        <details open={!large}>
          <summary>Parameters</summary>
          <ParamTree value={rule.params} />
        </details>
      )}
    </article>
  );
}
