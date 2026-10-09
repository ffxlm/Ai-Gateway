# Progress Log

Agent working memory. Read this first; update it after every completed unit of work.
Newest entries on top. Keep it short — facts, not prose.

## 2026-10-09

- [done] Fixed premium wallet admission control (root cause of the
  `hexiiii0743` "money left but 402" report). Two bugs in
  `app/routers/gateway.py`: (1) the pre-flight hold priced the *whole* estimate
  at the **output** rate, over-reserving up to ~4x (input is 4x cheaper on the
  DeepSeek tier); (2) wallet-only mode demanded the balance cover the full
  worst case, so a wallet that could actually pay was rejected. Now the hold is
  priced per-side via `catalog.premium_worst_case_cost()` (input-first trial
  split, mirroring `record_usage`), and wallet-only mode is **use-until-zero**:
  any positive available balance admits the request, hold capped at available,
  only an empty wallet is rejected. Error paths already charged nothing and are
  now covered by tests.
- [done] Added `tests/test_billing.py` (9 tests, isolated temp DB, stubbed
  upstream): split pricing, trial input-first, use-until-zero admission, empty
  wallet 402, upstream error => no charge + hold released + trial untouched,
  trial-covered-at-zero-balance. Full suite 18/18 green.
- [done] Lowered `WALLET_RESERVATION_TTL_SECONDS` 600 → 300 (still > the 180s
  upstream timeout) and added a 60s background janitor (`expire_stale_reservations`
  in `user_service.py`, wired in `main.py` lifespan) so a crashed request's
  hold returns promptly even when idle.
- [done] `_sanitize_error` now logs public model + resolved upstream model + URL
  server-side, so an operator can tell a config mistake (wrong model id/URL)
  from a real outage. Never leaked to the client.
- [done] Bumped app version `1.0.0` → `1.1.0` (`app/main.py`) to mark the
  Linear-inspired UI redesign + focus-outline fix release.
- [done] Investigated Admin → Requests `Source = est.`: 39 rows have
  `usage_source='estimated'` (of ~5,365). Cause is upstream, not us — the free
  9Router bridge omits the final `usage` chunk on streaming responses even though
  we already send `stream_options={"include_usage": true}`. Estimated usage is
  never billed (`charge=0`) but still consumes the free trial. Not input-size
  related (seen from 38 → 300k-token inputs; users with tiny inputs also hit it).
- [next] Prefer/recover upstream usage for streams, or reconcile estimated
  streams against the provider.

## 2026-10-06

- [done] Added second premium xHigh model `` (Anthropic, $0.50/$2.50 per 1M,
  official $5.00/$25.00, save 90%, trial 100k tokens/day) in
  `app/core/catalog.py`; `max_output_tokens` intentionally omitted so it falls
  back to the global `PREMIUM_MAX_OUTPUT_TOKENS`. Added `claude` → `anthropic`
  to `_owner()` and the model to the README premium table.
- [done] Verified live: `GET /v1/models` + dashboard card render the new model
  (tier xhigh, owned_by anthropic, $0.50/$2.50, save 90%, trial 100,000); id
  resolves on 9Router (272 upstream models, ctx 200,000) and a real upstream
  chat completion returned 200/"OK".
- [fix] Dashboard display `name` now matches the model `id` exactly for BOTH
  premium models (was the placeholder "the model 4.6"; DeepSeek renamed from
  "DeepSeek V4.1 Flash").
- [done] Repo baseline reviewed: FastAPI gateway, SQLite WAL, catalog in
  `app/core/catalog.py`, settlement via `record_usage()` in
  `app/services/user_service.py`.
- [next] Add CSV export to Admin → Wallet Reconciliation (`app/routers/pages.py`).
- [next] Add a Thai-safe CSV encoding (UTF-8 BOM) so Excel doesn't mangle Thai text.
- [known bug] SlipOK verification timeout currently surfaces as HTTP 500; should be
  a retryable 503.
- [note] Free tier = 3 models, never billed. Premium = `deepseek-v4.1-flash` +
  ``, trial default 1,000,000 tokens/day (`` overrides to 100,000),
  then charged from wallet.
