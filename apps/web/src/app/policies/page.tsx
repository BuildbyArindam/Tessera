import { EmptyState } from "@/components/States";

export default function PoliciesPage() {
  return (
    <>
      <div className="page-head">
        <h1>Policies</h1>
      </div>
      <EmptyState title="Not built yet">The machine-readable rules every proposal is checked against.</EmptyState>
    </>
  );
}
