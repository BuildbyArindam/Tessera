import type { ReactNode } from "react";
import type { Remote } from "@/lib/useApi";

export function Skeleton({ height = 80, label = "Loading" }: { height?: number; label?: string }) {
  return (
    <div role="status" aria-live="polite" aria-label={label}>
      <div className="skeleton" style={{ height }} aria-hidden="true" />
      <span className="sr-only">{label}…</span>
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="card state">
      <strong>{title}</strong>
      {children ? <p className="hint">{children}</p> : null}
    </div>
  );
}

export function ErrorState({ message, retry }: { message: string; retry: () => void }) {
  return (
    <div className="state error" role="alert">
      <p style={{ margin: "0 0 12px" }}>{message}</p>
      <button type="button" onClick={retry}>
        Try again
      </button>
    </div>
  );
}

/** Render the right one of loading / error / empty / ready for a section, so every section behaves the same. */
export function Async<T>({
  remote,
  isEmpty,
  empty,
  skeleton,
  children,
}: {
  remote: Remote<T>;
  isEmpty: (data: T) => boolean;
  empty: ReactNode;
  skeleton?: ReactNode;
  children: (data: T) => ReactNode;
}) {
  if (remote.status === "loading") return <>{skeleton ?? <Skeleton />}</>;
  if (remote.status === "error") return <ErrorState message={remote.message} retry={remote.retry} />;
  if (isEmpty(remote.data)) return <>{empty}</>;
  return <>{children(remote.data)}</>;
}
