# Progress Log

Agent working memory. Read this first; update it after every completed unit of work.
Newest entries on top. Keep it short — facts, not prose.

## 2026-10-06

- [done] Added second premium xHigh model `` (Anthropic, $0.50/$2.50 per 1M,
  official $5.00/$25.00, save 90%, trial 100k tokens/day) in
  `app/core/catalog.py`; `max_output_tokens` intentionally omitted so it falls
  back to the global `PREMIUM_MAX_OUTPUT_TOKENS`. Added `claude` → `anthropic`
  to `_owner()` and the model to the README premium table.
- [next] Verify `` actually resolves upstream on 9Router (id is sent verbatim).
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
