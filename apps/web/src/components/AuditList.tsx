import type { AuditEvent } from "@/api/client";
import { formatDateTime } from "@/lib/format";

const short = (hash: string) => hash.slice(0, 12);

/** The proposal an event concerns, when its payload names one. Payload text is data: it is only ever rendered as text. */
export const eventSubject = (e: AuditEvent): string => {
  const id = (e.payload as Record<string, unknown>).proposal_id;
  return typeof id === "string" ? id : "";
};

export function AuditList({
  events,
  selectedSeq,
  brokenSeq,
  onSelect,
}: {
  events: AuditEvent[];
  selectedSeq: number | null;
  brokenSeq: number | null;
  onSelect: (e: AuditEvent) => void;
}) {
  return (
    <div className="table-wrap">
      <table>
        <caption className="sr-only">Audit events, oldest first</caption>
        <thead>
          <tr>
            <th scope="col" className="r">#</th>
            <th scope="col">Time</th>
            <th scope="col">Event</th>
            <th scope="col">Proposal</th>
            <th scope="col">Hash</th>
            <th scope="col"><span className="sr-only">Details</span></th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.seq} className={e.seq === brokenSeq ? "broken" : undefined} aria-current={e.seq === selectedSeq ? "true" : undefined}>
              <th scope="row" className="r num">{e.seq}</th>
              <td>{formatDateTime(e.timestamp)}</td>
              <td>
                <span className="badge info">{e.event_type}</span>
                {e.seq === brokenSeq ? <span className="badge high"> chain broken here</span> : null}
              </td>
              <td className="mono">{eventSubject(e)}</td>
              <td className="mono">{short(e.hash)}</td>
              <td>
                <button type="button" aria-pressed={e.seq === selectedSeq} aria-label={`View event ${e.seq}`} onClick={() => onSelect(e)}>
                  View
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
