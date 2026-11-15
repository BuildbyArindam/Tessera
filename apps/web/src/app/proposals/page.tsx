import { EmptyState } from "@/components/States";

export default function ProposalsPage() {
  return (
    <>
      <div className="page-head">
        <h1>Proposals</h1>
      </div>
      <EmptyState title="Not built yet">Review and approve what the agent proposes.</EmptyState>
    </>
  );
}
