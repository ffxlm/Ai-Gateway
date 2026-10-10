# Progress Log

Agent working memory. Read this first; update it after every completed unit of work.
Newest entries on top. Keep it short — facts, not prose.

## 2026-10-10

- [done] Show the **InferHub account balance** on the reconcile card. The
  reconcile now also calls `GET /api/me` (same management API/key) and returns
  `account` (`balance_usdc`, `fiat_pending_usdc`, `email`, `status`) plus
  `account_error`. Both upstream calls run concurrently and each failure is
  reported, never raised, so the portal side is always returned. The card shows
  "InferHub balance: $X · pending $Y" (fetched when you press Run reconciliation).
  Bumped `console.js` cache-buster (`?v=10` → `?v=11`).
- [done] Clarified the reconcile **cross-check** line: it is labelled "whole
  account (all keys · full window, not just the portal)" and the card now warns
  when the measurement epoch clips the window start (`epoch_after_window_start`),
  because then the account total covers a longer period than the portal side and
  the gap mostly reflects that time mismatch, not a leak. Also bumped
  `console.js` cache-buster (`?v=8` → `?v=10`) — the reconcile card is rendered
  by that script, so a stale cached copy kept showing the old labels.
- [done] Added a **fresh measurement epoch** + honest business margin to the
  reconciliation. The provider only started returning per-request `usage.cost`
  recently, so every older row has `upstream_cost_usd = 0` and cannot be
  measured. New setting `metrics_epoch_start`: when set, the business figures
  count only requests at/after that instant; old rows are kept (customer billing
  history) but excluded from the *margin*. Admin API `POST /api/admin/metrics-epoch`
  (`{"action":"start"|"clear"}`) + a "Start measuring fresh" button on the
  reconcile card. The card now shows **Cost (measured)** and **Business margin**
  (`billed − measured cost`, self-measured so a shared upstream key no longer
  skews it), plus **Paid margin** / **Trial cost (free)** (the measured cost split
  in proportion to paid vs trial tokens) and the account **cross-check** as the
  leak signal.
- [done] Estimated requests (upstream never reported usage) are now priced at the
  measured average cost per token of the same model, so an unverifiable request
  still shows a cost instead of silently looking free.
- [note] Live E2E: a real premium call recorded `upstream_cost_usd = 1e-05`;
  with an epoch set just before it, reconcile counted 1 request, split it as
  trial cost (paid 0), and reported business margin from the measured cost.
- [done] Added **read-only premium margin reconciliation** (`app/services/reconcile_service.py`,
  admin API `GET /api/admin/reconcile?days=N`, card in Admin → Reconciliation).
  It compares three numbers for a UTC day window: portal billed
  (`Σ request_logs.cost_usd`), portal-observed upstream (`Σ upstream_cost_usd`),
  and the upstream account's own spend (`InferHub /api/usage/logs` totals).
  Gross margin = billed − paid. The observed-vs-account **gap** is the key
  signal: it exposes usage billed to us that the portal never saw (estimated
  streams, or the key being used outside the portal). Deliberately aggregate and
  billing-neutral — no per-request back-charge (cannot be matched reliably and
  would break the "price shown = price charged" contract). New setting
  `PREMIUM_MANAGEMENT_URL` (default `https://inferhub.dev/api`).
- [note] Live check exposed exactly that signal: the account shows 6,436
  upstream requests / 0.5745 USDC in 7 days while this (dev) portal.db logged
  62 premium rows, all stale (Oct 4–6). Reconciliation is only meaningful when
  the portal is the sole consumer of the upstream key — use a dedicated key.
- [done] Added `@source "../static/console.js"` to `tailwind.input.css` and
  rebuilt `portal.css`, so utility classes emitted by the console script (not
  just templates) are compiled.
- [done] Added prompt-cache pass-through billing for premium models. InferHub
  bills a cache-read input token at 0.1x the input ask (measured from
  `usage.cost` on cb/deepseek-v4.1-flash, cb/gpt-6-sol, cb/gpt-6-astra:
  0.099–0.100x). We now pass the same ratio to customers so the cache margin
  equals the normal margin instead of being captured. `catalog.py`: added
  `CACHE_INPUT_RATIO`, per-model `price_cached_in_usd`, `premium_cached_input_price()`
  and a `tokens_cached` arg on `premium_price()` (defaults to full input rate when
  a model has no verified cache rate, so we never sell below cost).
- [done] `proxy_service._token_breakdown()` now returns
  `(prompt, cached, completion)`, reading every cache alias
  (`prompt_cache_hit_tokens`, `cached_tokens`, `prompt_tokens_details.cached_tokens`,
  `cache_read_input_tokens`) and clamping to the prompt. Added `_upstream_cost()`
  to capture the provider's own `usage.cost`.
- [done] `record_usage()` bills cached tokens at the cached rate, keeps the
  trial split input-first (uncached before cached, then output) consistent with
  `premium_worst_case_cost` (which still holds at the all-uncached worst case),
  and writes `tokens_cached`, `cache_savings_usd`, `upstream_cost_usd` to
  `request_logs` (new idempotent migration + admin CSV columns).
- [done] Verified live end-to-end. Non-streaming: 2 identical 40k-token calls,
  call 2 hit 99.8% cache, charged $0.0000804 (predicted exactly), saved the
  customer $0.000534, margin 8.0x vs provider cost. Streaming: usage carries the
  cache split via `prompt_tokens_details.cached_tokens` only on a cache hit
  (absent otherwise), captured correctly (34,944/35,033 cached, charge matched).
  Note: streaming usage omits the richer fields; `usage.cost` is floored at
  1e-5 USDC, and routing ask varies ~10x per request (bounded by the 98% bid
  floor). Tests: 33/33 green.
- [watch] `minDiscountPct=98.00` causes `402 no_provider_under_bid` on busy
  models (seen on alicn/deepseek-v4.1-flash) — tune per-model budget rather than
  removing the floor (the floor is what keeps buy ≤ 2% of official).

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
