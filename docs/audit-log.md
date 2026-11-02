# The audit log

`audit_events` is an append-only, hash-chained table. Code: `packages/audit/tessera_audit/`. Table and trigger:
`migrations/versions/0001_initial_schema.py`.

## What a row is

| column | meaning |
| --- | --- |
| `seq` | 1, 2, 3, ... with no gaps. The primary key. |
| `timestamp` | When the event was written (UTC, microseconds). |
| `event_type` | One of ten: `forecast_made`, `proposal_created`, `policy_decision`, `approval_requested`, `approved`, `rejected`, `execution_submitted`, `execution_result`, `webhook_received`, `autonomy_changed`. |
| `payload` | The exact canonical JSON text that was hashed (sorted keys, no spaces, money as strings). `TEXT`, not `JSONB`, because `JSONB` reorders keys and rewrites numbers. |
| `prev_hash` | The previous row's `hash`; 64 zeros for the first row. Unique, so the chain cannot fork. |
| `hash` | `sha256(prev_hash + envelope)`, where the envelope is `{"payload": ..., "seq": n, "ts": "...", "type": "..."}`. |

The brief says `sha256(prev_hash + canonical_payload)`. The hash here also covers `seq`, `timestamp` and `event_type`.
With the payload alone, someone could relabel an event or change its time and the chain would still verify.

## What emits what (today)

Emitted now: `proposal_created`, `policy_decision`, `approval_requested` (by `POST /plan/run`), `approved` and `rejected`
(by the approve and reject routes). Defined but emitted by nobody yet: `forecast_made` (the worker, Day 13; the payload
builder exists), `execution_submitted`, `execution_result`, `webhook_received` (Day 13), `autonomy_changed` (Day 14).

A planning run that produces no proposals (the model failed, or its output was invalid) writes no event: the list of
event types is fixed and has none for it. The API response says what happened (`outcome`).

## How rows are written

`AuditLog.append` takes a transaction-scoped advisory lock, reads the newest row, and inserts the next one. Two writers
cannot choose the same `seq`; the second waits until the first commits or rolls back. A rolled-back append leaves no gap.
The event commits or rolls back together with the rows it describes. Keep transactions short: a long one blocks others.

## What the database refuses

A row trigger rejects `UPDATE` and `DELETE`, and a statement trigger rejects `TRUNCATE`, with error code 23001
(`restrict_violation`). Check constraints refuse an unknown `event_type`, a malformed hash, or `seq < 1`.

## What `verify_chain()` proves, and what it does not

It walks every row in order and stops at the first one that fails: a `seq` that is missing or out of order, a `prev_hash`
that does not match the row before, or a `hash` that does not match the row's own contents. It names that `seq`
(for a missing row, the first `seq` that should exist and does not). `GET /audit/verify` returns the same.

It cannot detect:

1. **Removal of the newest rows.** Nothing later points back at them. Fix: publish `head()` (the newest `seq` and `hash`)
   somewhere the database owner cannot edit (a signed log, an object store with retention, an email), and compare.
2. **A complete rewrite.** Someone who edits a row and then recomputes every hash after it produces a valid chain. The same
   published head exposes it.
3. **Anything done while the trigger is off.** The table's owner or a superuser can `ALTER TABLE ... DISABLE TRIGGER`
   or `SET session_replication_role = replica`. The chain still catches an edit made that way (see
   `tests/integration/test_audit_db.py`), but the trigger itself is not a barrier against those roles.

## Recommended database roles (production)

Run migrations as an owner role. Run the API and the worker as a different role that owns nothing and can only append:

```sql
CREATE ROLE tessera_app LOGIN PASSWORD '...';
GRANT SELECT, INSERT ON audit_events TO tessera_app;      -- no UPDATE, DELETE or TRUNCATE
GRANT SELECT, INSERT, UPDATE ON proposals, approvals, executions TO tessera_app;
GRANT SELECT, INSERT ON businesses, invoices, payments, forecasts, policy_decisions TO tessera_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO tessera_app;
```

These grants are a starting point and are NOT exercised by any test: the development and test databases run as a superuser,
so nothing here proves the API works under this role. `docker-compose.yml` is for development only.
