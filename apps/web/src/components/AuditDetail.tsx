import { useEffect, useRef } from "react";
import type { AuditEvent } from "@/api/client";
import { formatDateTime } from "@/lib/format";

export function AuditDetail({ event, onClose }: { event: AuditEvent; onClose: () => void }) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => heading.current?.focus(), [event.seq]); // move keyboard and screen-reader focus to what was opened

  return (
    <section className="card detail" aria-labelledby="audit-detail-h" data-testid="audit-detail">
      <div className="detail-head">
        <h2 id="audit-detail-h" tabIndex={-1} ref={heading}>
          Event #{event.seq} · {event.event_type}
        </h2>
        <button type="button" onClick={onClose}>
          Close
        </button>
      </div>
      <dl className="facts">
        <dt>Recorded</dt>
        <dd>{formatDateTime(event.timestamp)}</dd>
        <dt>Previous hash</dt>
        <dd className="mono hash">{event.prev_hash}</dd>
        <dt>This hash</dt>
        <dd className="mono hash">{event.hash}</dd>
      </dl>
      <h3>Payload</h3>
      <pre className="payload" tabIndex={0} aria-label="Event payload">
        {JSON.stringify(event.payload, null, 2)}
      </pre>
      <p className="hint">Each hash covers the previous hash and this payload, so changing any past event breaks every hash after it.</p>
    </section>
  );
}
