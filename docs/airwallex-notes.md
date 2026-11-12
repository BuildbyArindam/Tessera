# Airwallex API notes

Read from the official reference on 2026-11-02 (the reference lists `2026-08-21` as the latest API version). Every row below says where it came from. **VERIFIED** means the page stated it. **UNVERIFIED** means no page I could read stated it; the sandbox client treats those as unknowns, never as facts.

Scope: Day 12 uses authentication, balances, FX quotes and conversions. Beneficiary, transfer, card and webhook rows are left for Day 13.

## Environments

| | URL | Status |
| --- | --- | --- |
| Sandbox | `https://api.sandbox.airwallex.com` | VERIFIED ([API intro](https://www.airwallex.com/docs/api), [login](https://www.airwallex.com/docs/api/2024-04-30/authentication/api_access_token/login)) |
| Production | `https://api.airwallex.com` | VERIFIED (API intro) |

`.env.example` used to say `https://api-demo.airwallex.com`. The reference calls the test environment "Sandbox" and gives the host above, so the example was corrected on Day 12.

## Endpoints used by the Day 12 client

| Area | Endpoint | Request | Response fields we read | Source |
| --- | --- | --- | --- | --- |
| Authentication | `POST /api/v1/authentication/login` | headers `x-client-id`, `x-api-key` (required); `x-login-as` (optional, when the key covers several accounts). No body. | `token`, `expires_at` (ISO 8601, example `2021-06-18T16:30:00+0000`). Token valid 30 minutes; the page says to reuse it until `expires_at`. 201 on success. | VERIFIED, [login](https://www.airwallex.com/docs/api/2024-04-30/authentication/api_access_token/login) |
| Balances | `GET /api/v1/balances/current` | optional query `account_type` = `cash`, `yield` or `credit`. Optional header `x-on-behalf-of` (not used). | A top-level JSON **array**, one object per currency and account type: `account_type`, `currency`, `available_amount`, `pending_amount`, `prepayment_amount`, `reserved_amount`, `total_amount`. Amounts are JSON **numbers**. | VERIFIED, [balances](https://www.airwallex.com/docs/api/2022-09-09/core_resources/balances/current) (this page is the 2022-09-09 version; no newer balances page was reachable) |
| FX quote, create | `POST /api/v1/fx/quotes/create` | `buy_currency`, `sell_currency`, `validity` (all required; `validity` is one of `MIN_1`, `MIN_15`, `MIN_30`, `HR_1`, `HR_4`, `HR_8`, `HR_24`); exactly one of `sell_amount` / `buy_amount` (JSON **number**). | `quote_id`, `client_rate`, `mid_rate`, `awx_rate`, `buy_amount`, `buy_currency`, `sell_amount`, `sell_currency`, `valid_from_at`, `valid_to_at` (both inclusive; there is no `expires` field), `validity`, `usage` (`SINGLE_USE` or `MULTI_USE`). 201. | VERIFIED, [quotes](https://www.airwallex.com/docs/api/transactional_fx/quotes/create) |
| FX quote, retrieve | `GET /api/v1/fx/quotes/{quote_id}` | none | same fields as create. 404 `not_found`. | VERIFIED, same page |
| Conversion, create | `POST /api/v1/fx/conversions/create` | `buy_currency`, `sell_currency`, `request_id` (required, max 64 chars); exactly one of `sell_amount` / `buy_amount` (**string**); optional `quote_id`, `conversion_date`, `funding_source`, `metadata`. | `conversion_id`, `request_id`, `quote_id`, `buy_currency`, `buy_amount`, `sell_currency`, `sell_amount` (numbers), `client_rate`, `created_at`, `status`. 201. | VERIFIED, [conversion create](https://www.airwallex.com/docs/api/transactional_fx/conversion/create) |
| Conversion, list | `GET /api/v1/fx/conversions` | optional query `request_id`, `status`, `buy_currency`, `sell_currency`, `from_created_at`, `to_created_at`, `page_num` (from 0), `page_size` (default 100). With no date filter the window is the last 31 days. | `{ "has_more": bool, "items": [conversion...] }`, newest first. | VERIFIED, [conversions](https://www.airwallex.com/docs/api/transactional_fx/conversion) |
| Conversion, retrieve | `GET /api/v1/fx/conversions/{conversion_id}` | none | same fields as create. | VERIFIED, same page |

Conversion `status` values: `SCHEDULED`, `CANCELLED`, `OVERDUE`, `SETTLED`.

**Version drift to be aware of.** The 2024-04-30 reference documents a different conversion API: `POST /api/v1/conversions/create`, with required `reason` and `term_agreement` fields and statuses `AWAITING_FUNDS`, `PENDING_SETTLEMENT`, and so on. The client targets the current (unversioned) pages above. If a live call returns 404 or `invalid_endpoint`, the first suspect is the API version (see "Unknown" below).

## Error codes (VERIFIED, [FX error codes](https://www.airwallex.com/docs/transactional-fx/troubleshooting/error-codes) and the endpoint pages)

| HTTP | Code | Port error |
| --- | --- | --- |
| 400 | `expired_quote` | `QuoteExpiredError` |
| 400 | `insufficient_funds`, `credit_limit_exceeded` | `InsufficientFundsError` |
| 400 | `invalid_quote_id` | `NotFoundError` |
| 400 | `amount_below_limit`, `amount_above_limit`, `conflict_original_ccy`, `field_required`, `invalid_argument`, `invalid_conversion_date`, `invalid_settlement_date`, `unsupported_currency`, `validation_error`, `invalid_parameter`, `operation_failed` | `InvalidRequestError` (also exported as `ValidationError`) |
| 401 | `credentials_invalid`, `credentials_expired` | `AuthError` |
| 403 | `forbidden` | `AuthError` |
| 404 | `not_found` | `NotFoundError` |
| 429 | `too_many_requests` | `RateLimitedError` |
| 500 | `service_unavailable` | `UnknownError` (retried) |

Limits on conversions: minimum USD 1 equivalent, default maximum USD 5,000,000 equivalent (error-codes page; both can be changed by the account manager).

## Idempotency

`request_id` on conversion create is the only documented idempotency key. The page says it "must be unique per request attempt" and that conversion requests with the same `request_id` "will be rejected". Quotes, balances and login take no key.

Consequence for the port contract (a replayed key returns the original result): a duplicate is **rejected**, not replayed, so the adapter reconciles. Before creating, and again after any ambiguous or unrecognised failure, it lists conversions with `request_id=...` and returns the one it finds (checking `quote_id` matches, else `IdempotencyConflictError`).

## Unknown (UNVERIFIED: no page I could read states these)

- **API version header.** The login page does not mention `x-api-version`; the intro lists versions without saying how to choose one or what the default is. The client sends `x-api-version` only if `AIRWALLEX_API_VERSION` is set. The header name is from memory, not from a page. **First thing to check with real credentials.**
- **Error body shape.** No page I could read shows the JSON error body. The client reads a top-level string `code` and `message` if present and falls back to the HTTP status alone.
- **What a duplicate `request_id` returns** (status and code). Handled by reconciling on any unrecognised 4xx instead of matching a code.
- **Whether `request_id` filtering on the list endpoint is exact.** The client re-checks `request_id` on every returned item.
- **Rate-limit headers.** `Retry-After` is read if present; none is documented.
- **How `sell_amount` must relate to `quote_id` on conversion create.** The page says currencies must match the quote and says nothing about amounts. The client sends the quote's own `sell_amount`.
- **Direction of `client_rate`.** Not stated. The client derives the buy-per-sell rate from the amounts and refuses a quote whose rate matches neither direction.
- **Newer balances page.** Only the 2022-09-09 page was reachable.
- **Sandbox limits** beyond the conversion limits above (rate limits, wallet funding for tests).

## Differences between mock and sandbox

Checked on Day 12 two ways: the shared contract suite (`tests/contract`, 18 tests, run on the mock, on the sandbox client over `tests/fake_airwallex.py`, and live when credentials are set) and a side-by-side run of 14 edge cases through both adapters. **The sandbox column below was measured only against the fake server; nothing was run against the real sandbox.** Rows marked "live: unknown" need a credentialed run.

Fixed so the two now agree (each has a contract test):

| Behaviour | Was | Now |
| --- | --- | --- |
| Quote at exactly its expiry instant | mock refused it, sandbox client accepted it (the reference calls `valid_to_at` inclusive) | both refuse (`QuoteExpiredError`); the client never submits on the last instant |
| Whitespace-only idempotency key | mock accepted it, sandbox client refused it | both raise `InvalidRequestError` |
| Amount too small to convert (buy side rounds to 0.00) | mock `InvalidRequestError`, sandbox client `UnknownError` | both `InvalidRequestError` |

Still different, on purpose:

| Method / behaviour | Mock | Sandbox client |
| --- | --- | --- |
| `create_beneficiary`, `create_transfer`, `create_card`, `set_card_limits` | implemented | `NotImplementedError` until Day 13 |
| `execute_conversion`, key replayed | stored result | vendor rejects a repeated `request_id`; the client finds the conversion with `GET /api/v1/fx/conversions?request_id=...` and returns it (one extra call per execution) |
| `execute_conversion`, replayed conversion is `CANCELLED` | n/a | `InvalidRequestError` |
| Idempotency key format | any non-blank string | keys of 1 to 64 characters from `[A-Za-z0-9_-]` are sent as is; anything else is replaced by its SHA-256 (64 hex chars) |
| Quote lifetime | `quote_ttl_seconds`, default 30 | `validity` `MIN_1` (60 s), the shortest the reference offers |
| Conversion amount limits | none | the vendor enforces a minimum of about USD 1 and a default maximum of USD 5,000,000 (`amount_below_limit` / `amount_above_limit`, mapped to `InvalidRequestError`). Neither the mock nor the fake enforces them, so the contract suite cannot test them offline. |
| Balances after a conversion | change at once | live: unknown (conversions come back `SCHEDULED`; the contract test for it is skipped live) |
| Balances response | one cash balance per supported currency | non-`cash` account types and currencies Tessera does not trade are ignored, amounts of the same currency are summed |
| Reusing a quote under a new key | `InvalidRequestError` | live: unknown (docs say quotes are `SINGLE_USE`); the fake rejects it |
| `executed_at` | the mock's clock | the vendor's `created_at` |
| Ids | `mock-quote-1`, ... | vendor ids, passed through |
| Unknown quote id | `NotFoundError` | `NotFoundError`; an id with characters outside `[A-Za-z0-9_-]` is refused without a request |
