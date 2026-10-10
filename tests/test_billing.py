"""Billing correctness tests: admission control, settlement and error paths.

Every test runs against a throwaway SQLite database (settings.DATABASE_PATH is
patched before the app is imported by the test), so neither the developer's
portal.db nor production is ever touched. The upstream HTTP client is replaced
with a stub, so no network call is made.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core import config as config_module
from app.core.catalog import (
    premium_worst_case_cost,
    premium_input_price,
    premium_output_price,
    premium_cached_input_price,
    premium_price,
    PREMIUM_MODELS,
    CACHE_INPUT_RATIO,
)
from app.core.database import init_db
from app.services.user_service import get_today_str

DEEPSEEK = "deepseek-v4.1-flash"  # $0.015 in / $0.06 out per 1M tokens
API_KEY = "sk-portal-test-billing"

# Business constants that make the no-loss guarantee explicit:
#   * we sell premium tokens at SELL_DISCOUNT_PCT off the official list price,
#   * InferHub's bid floor caps what we pay at (1 - FLOOR_PCT) * official,
#   * the provider bills a prompt-cache read at CACHE_BUY_RATIO of its input ask.
SELL_DISCOUNT_PCT = 90
FLOOR_PCT = 97
CACHE_BUY_RATIO = 0.10


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.content = self.text.encode()
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Minimal stand-in for httpx.AsyncClient (non-streaming POST only)."""

    response = None

    def __init__(self, *args, **kwargs):
        pass

    async def post(self, url, headers=None, json=None):
        return self.response

    async def aclose(self):
        pass


class BillingUnitTests(unittest.TestCase):
    def test_split_pricing_prices_input_and_output_separately(self):
        self.assertAlmostEqual(premium_input_price(DEEPSEEK), 0.015)
        self.assertAlmostEqual(premium_output_price(DEEPSEEK), 0.06)
        self.assertAlmostEqual(premium_worst_case_cost(DEEPSEEK, 1_000_000, 0), 0.015, places=9)
        self.assertAlmostEqual(premium_worst_case_cost(DEEPSEEK, 0, 1_000_000), 0.06, places=9)
        self.assertAlmostEqual(premium_worst_case_cost(DEEPSEEK, 1_000_000, 1_000_000), 0.075, places=9)

    def test_new_hold_is_cheaper_than_the_old_blended_formula(self):
        est_in, est_out = 318_000, 32_768
        old = (est_in + est_out) * (premium_output_price(DEEPSEEK) / 1e6)
        new = premium_worst_case_cost(DEEPSEEK, est_in, est_out)
        self.assertLess(new, old)
        self.assertAlmostEqual(new, est_in * 0.015 / 1e6 + est_out * 0.06 / 1e6, places=9)

    def test_trial_is_applied_input_first(self):
        # 1M trial tokens fully cover 1M input; the output side is then billed.
        self.assertAlmostEqual(
            premium_worst_case_cost(DEEPSEEK, 1_000_000, 1_000_000, trial_remaining=1_000_000),
            0.06, places=9,
        )
        # Trial larger than the whole request => nothing to bill.
        self.assertEqual(
            premium_worst_case_cost(DEEPSEEK, 100, 100, trial_remaining=1_000_000), 0.0
        )

    def test_unknown_model_costs_nothing(self):
        self.assertEqual(premium_worst_case_cost("nope", 1_000_000, 1_000_000), 0.0)


class CachePricingTests(unittest.TestCase):
    """The cache pass-through must be cheaper for the customer and still profitable."""

    def test_cached_input_price_is_a_tenth_of_input(self):
        self.assertAlmostEqual(premium_cached_input_price(DEEPSEEK), 0.0015, places=9)
        self.assertAlmostEqual(premium_cached_input_price("gpt-6-sol"), 0.02, places=9)
        self.assertAlmostEqual(premium_cached_input_price("gpt-6-astra"), 0.10, places=9)

    def test_unknown_model_has_no_cache_discount(self):
        # No verified cache rate => full input price (never sell below cost).
        self.assertEqual(premium_cached_input_price("nope"), 0.0)

    def test_cached_tokens_cost_a_tenth(self):
        full = premium_price(DEEPSEEK, 1_000_000, 0)
        cached = premium_price(DEEPSEEK, 1_000_000, 0, tokens_cached=1_000_000)
        self.assertAlmostEqual(full, 0.015, places=9)
        self.assertAlmostEqual(cached, 0.0015, places=9)
        self.assertLess(cached, full)

    def test_cached_count_is_clamped_to_prompt(self):
        # A lying upstream reporting more cached than prompt must not create a
        # negative uncached count (which would under-bill).
        self.assertAlmostEqual(
            premium_price(DEEPSEEK, 1_000_000, 0, tokens_cached=5_000_000), 0.0015, places=9
        )

    def test_every_model_keeps_a_positive_margin_at_the_configured_floor(self):
        # No-loss invariant: we sell at SELL_DISCOUNT_PCT off official and buy at
        # no more than (1 - FLOOR) * official (the InferHub bid floor). As long as
        # FLOOR >= SELL_DISCOUNT, every side (input, output, cache) is profitable.
        self.assertGreaterEqual(FLOOR_PCT, SELL_DISCOUNT_PCT)
        floor = FLOOR_PCT / 100.0
        for m in PREMIUM_MODELS:
            with self.subTest(model=m["id"]):
                self.assertGreater(m["price_in_usd"], (1 - floor) * m["official_in_usd"])
                self.assertGreater(m["price_out_usd"], (1 - floor) * m["official_out_usd"])
                self.assertGreater(
                    premium_cached_input_price(m["id"]),
                    (1 - floor) * m["official_in_usd"] * CACHE_BUY_RATIO,
                )

    def test_cache_is_margin_neutral(self):
        # Upstream bills a cache read at 0.1x its input ask and we sell it at
        # 0.1x our input price, so the cache ratio cancels out and the cache
        # never changes the no-loss condition -- only the floor does.
        for m in PREMIUM_MODELS:
            with self.subTest(model=m["id"]):
                self.assertAlmostEqual(
                    premium_cached_input_price(m["id"]) / m["price_in_usd"],
                    CACHE_INPUT_RATIO,
                    places=9,
                )


class BillingEndpointTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = tempfile.mkdtemp(prefix="aigw-billing-")
        self.db_path = os.path.join(self.tmp, "test.db")
        self.stack.enter_context(
            patch.object(config_module.settings, "DATABASE_PATH", self.db_path)
        )
        init_db()

        from app.main import app
        self.client = self.stack.enter_context(TestClient(app))

    # ── helpers ──────────────────────────────────────────────────────────────

    def test_daily_trial_settings_preserve_defaults_and_allow_overrides(self):
        from app.core.database import update_setting
        from app.core.catalog import premium_trial_setting_key
        from app.services.user_service import premium_trial_limit

        update_setting("premium_trial_tokens_per_day", "1000000")
        self.assertEqual(premium_trial_limit(DEEPSEEK), 1000000)
        self.assertEqual(premium_trial_limit("gpt-6-sol"), 200000)
        self.assertEqual(premium_trial_limit("gpt-6-astra"), 50000)
        update_setting(premium_trial_setting_key("gpt-6-sol"), "12345")
        update_setting(premium_trial_setting_key("gpt-6-astra"), "0")
        update_setting(premium_trial_setting_key(DEEPSEEK), "98765")
        self.assertEqual(premium_trial_limit("gpt-6-sol"), 12345)
        self.assertEqual(premium_trial_limit("gpt-6-astra"), 0)
        self.assertEqual(premium_trial_limit(DEEPSEEK), 98765)
        update_setting(premium_trial_setting_key("gpt-6-sol"), "invalid")
        self.assertEqual(premium_trial_limit("gpt-6-sol"), 200000)

    def test_admin_saves_model_trials_without_resetting_usage(self):
        from app.core.catalog import premium_trial_setting_key
        from app.services.user_service import premium_trial_limit, premium_trial_used

        self._seed_user(trial_used=1234)
        with patch("app.routers.pages.get_session_user", return_value={"role": "admin"}):
            response = self.client.post("/api/admin/settings", data={
                premium_trial_setting_key(DEEPSEEK): "500",
                premium_trial_setting_key("gpt-6-sol"): "300000",
                premium_trial_setting_key("gpt-6-astra"): "0",
            }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/admin#settings")
        self.assertEqual(premium_trial_limit(DEEPSEEK), 500)
        self.assertEqual(premium_trial_limit("gpt-6-sol"), 300000)
        self.assertEqual(premium_trial_limit("gpt-6-astra"), 0)
        self.assertEqual(premium_trial_used("u-hex", DEEPSEEK), 1234)

    def test_admin_trial_validation_prevents_partial_updates(self):
        from app.core.database import get_setting
        from app.core.catalog import premium_trial_setting_key

        for invalid in ["-1", "1.5", "abc", ""]:
            with self.subTest(value=invalid), patch("app.routers.pages.get_session_user", return_value={"role": "admin"}):
                response = self.client.post("/api/admin/settings", data={
                    "usd_to_thb": "999", premium_trial_setting_key("gpt-6-sol"): invalid,
                }, follow_redirects=False)
                self.assertEqual(response.status_code, 400)
                self.assertNotEqual(get_setting("usd_to_thb"), "999")
        with patch("app.routers.pages.get_session_user", return_value={"role": "admin"}):
            response = self.client.post("/api/admin/settings", data={premium_trial_setting_key("unknown"): "1"})
            self.assertEqual(response.status_code, 400)
        with patch("app.routers.pages.get_session_user", return_value={"role": "user"}):
            response = self.client.post("/api/admin/settings", data={premium_trial_setting_key(DEEPSEEK): "0"})
            self.assertEqual(response.status_code, 403)

    def _query(self, sql, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _seed_user(self, balance=0.01577044, trial_used=0):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO users (id, username, api_key, role, tier, balance) "
                "VALUES (?, ?, ?, 'user', 'free', ?)",
                ("u-hex", "hexiiii0743", API_KEY, balance),
            )
            if trial_used:
                conn.execute(
                    "INSERT INTO model_trial_usage (user_id, model, usage_date, tokens_used) "
                    "VALUES (?, ?, ?, ?)",
                    ("u-hex", DEEPSEEK, get_today_str(), trial_used),
                )
            conn.commit()
        finally:
            conn.close()

    def _post(self, prompt_chars, model=DEEPSEEK):
        return self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": model, "messages": [{"role": "user", "content": "x" * prompt_chars}]},
        )

    def _mock_upstream(self, response):
        _FakeAsyncClient.response = response
        return patch("app.services.proxy_service.httpx.AsyncClient", _FakeAsyncClient)

    def _balance(self):
        return self._query("SELECT balance FROM users WHERE id = 'u-hex'")[0]["balance"]

    # ── tests ────────────────────────────────────────────────────────────────

    def test_use_until_zero_admits_a_small_balance(self):
        """Trial exhausted, balance $0.0158, long prompt: must be admitted, not 402.

        This is the exact hexiiii0743 regression: the old blended-rate hold
        (~$0.01997) exceeded the balance even though the real cost (~$0.0075) did not.
        """
        self._seed_user(balance=0.01577044, trial_used=1_000_000)
        response = _FakeResponse(200, {
            "id": "chatcmpl-1", "object": "chat.completion", "model": "cb/deepseek-v4.1-flash",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 500_000, "completion_tokens": 100},
        })
        with self._mock_upstream(response):
            r = self._post(prompt_chars=1_200_000)

        self.assertEqual(r.status_code, 200, r.text)
        expected_charge = 500_000 * 0.015 / 1e6 + 100 * 0.06 / 1e6
        self.assertAlmostEqual(self._balance(), 0.01577044 - expected_charge, places=9)
        self.assertGreater(self._balance(), 0.0)

    def test_empty_wallet_is_rejected(self):
        self._seed_user(balance=0.0, trial_used=1_000_000)
        response = _FakeResponse(200, {"id": "x", "usage": {"prompt_tokens": 10, "completion_tokens": 1}})
        with self._mock_upstream(response):
            r = self._post(prompt_chars=40_000)
        self.assertEqual(r.status_code, 402, r.text)
        self.assertEqual(self._balance(), 0.0)

    def test_upstream_error_charges_nothing_and_releases_hold(self):
        self._seed_user(balance=0.01577044, trial_used=1_000_000)
        with self._mock_upstream(_FakeResponse(500, {"error": "boom"})):
            r = self._post(prompt_chars=1_200_000)

        self.assertEqual(r.status_code, 502, r.text)  # remapped, never leaks upstream
        self.assertAlmostEqual(self._balance(), 0.01577044, places=9)
        self.assertEqual(self._query("SELECT * FROM wallet_transactions"), [])
        active = self._query("SELECT * FROM wallet_reservations WHERE status = 'active'")
        self.assertEqual(active, [])

    def test_upstream_error_does_not_consume_the_free_trial(self):
        self._seed_user(balance=1.0, trial_used=0)
        with self._mock_upstream(_FakeResponse(429, {"error": "rate limited"})):
            r = self._post(prompt_chars=40_000)
        self.assertEqual(r.status_code, 429, r.text)
        rows = self._query(
            "SELECT * FROM model_trial_usage WHERE user_id = 'u-hex' AND model = ?", (DEEPSEEK,)
        )
        self.assertEqual(rows, [])
        self.assertAlmostEqual(self._balance(), 1.0, places=9)

    def test_trial_covers_request_even_with_zero_balance(self):
        self._seed_user(balance=0.0, trial_used=0)
        response = _FakeResponse(200, {
            "id": "chatcmpl-2", "object": "chat.completion", "model": "cb/deepseek-v4.1-flash",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 50},
        })
        with self._mock_upstream(response):
            r = self._post(prompt_chars=4_000)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._balance(), 0.0)  # trial covered it, nothing billed
        used = self._query(
            "SELECT tokens_used FROM model_trial_usage WHERE user_id = 'u-hex' AND model = ?",
            (DEEPSEEK,),
        )
        self.assertEqual(used[0]["tokens_used"], 1050)

    def test_cache_hit_is_billed_at_the_cached_rate_and_logged(self):
        """A cache hit must cost less than an all-uncached prompt and be auditable."""
        self._seed_user(balance=1.0, trial_used=1_000_000)
        response = _FakeResponse(200, {
            "id": "chatcmpl-cache", "object": "chat.completion", "model": "cb/deepseek-v4.1-flash",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100_000,
                "prompt_cache_hit_tokens": 90_000,
                "completion_tokens": 1_000,
                "cost": 0.0003,
            },
        })
        with self._mock_upstream(response):
            r = self._post(prompt_chars=400_000)

        self.assertEqual(r.status_code, 200, r.text)
        expected = (10_000 * 0.015 + 90_000 * 0.0015 + 1_000 * 0.06) / 1e6
        self.assertAlmostEqual(self._balance(), 1.0 - expected, places=9)

        row = self._query(
            "SELECT * FROM request_logs WHERE model = ? ORDER BY id DESC LIMIT 1", (DEEPSEEK,)
        )[0]
        self.assertEqual(row["tokens_in"], 100_000)
        self.assertEqual(row["tokens_cached"], 90_000)
        self.assertAlmostEqual(row["cost_usd"], expected, places=9)
        self.assertAlmostEqual(row["upstream_cost_usd"], 0.0003, places=9)
        # Discount given vs. charging the full input rate for cached tokens.
        self.assertAlmostEqual(row["cache_savings_usd"], 90_000 * (0.015 - 0.0015) / 1e6, places=9)

    def test_cache_hit_is_cheaper_than_no_cache_for_the_same_tokens(self):
        usage_common = {"prompt_tokens": 100_000, "completion_tokens": 1_000}

        def _reset():
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("UPDATE users SET balance = 1.0 WHERE id = 'u-hex'")
                conn.execute("DELETE FROM model_trial_usage WHERE user_id = 'u-hex'")
                conn.execute(
                    "INSERT INTO model_trial_usage (user_id, model, usage_date, tokens_used) "
                    "VALUES ('u-hex', ?, ?, 1000000)", (DEEPSEEK, get_today_str()),
                )
                conn.commit()
            finally:
                conn.close()

        self._seed_user(balance=1.0, trial_used=1_000_000)
        no_cache = _FakeResponse(200, {
            "id": "a", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                    "finish_reason": "stop"}],
            "usage": dict(usage_common),
        })
        with self._mock_upstream(no_cache):
            self._post(prompt_chars=400_000)
        charged_no_cache = 1.0 - self._balance()

        _reset()  # reset wallet + trial for a clean second measurement
        with_cache = _FakeResponse(200, {
            "id": "b", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                    "finish_reason": "stop"}],
            "usage": dict(usage_common, prompt_cache_hit_tokens=90_000),
        })
        with self._mock_upstream(with_cache):
            self._post(prompt_chars=400_000)
        charged_with_cache = 1.0 - self._balance()

        self.assertLess(charged_with_cache, charged_no_cache)
        self.assertAlmostEqual(charged_with_cache, 90_000 * 0.0015 / 1e6 + 10_000 * 0.015 / 1e6
                               + 1_000 * 0.06 / 1e6, places=9)

    def test_cache_aliases_are_understood(self):
        """[OI]-style prompt_tokens_details.cached_tokens is also a cache hit."""
        from app.services.proxy_service import _token_breakdown
        self.assertEqual(_token_breakdown({"prompt_tokens": 100, "cached_tokens": 80}), (100, 80, 0))
        self.assertEqual(
            _token_breakdown({"prompt_tokens": 100,
                              "prompt_tokens_details": {"cached_tokens": 70}}),
            (100, 70, 0),
        )
        self.assertEqual(
            _token_breakdown({"prompt_tokens": 100, "cache_read_input_tokens": 60}),
            (100, 60, 0),
        )
        # Cached can never exceed the prompt size.
        self.assertEqual(_token_breakdown({"prompt_tokens": 100, "cached_tokens": 999}), (100, 100, 0))


class _FakeReconcileClient:
    """Stands in for httpx.AsyncClient used by the reconciliation service."""

    payload = {}
    status = 200

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        outer = self

        class _Resp:
            status_code = outer.status

            def raise_for_status(self):
                if outer.status >= 400:
                    raise RuntimeError(f"HTTP {outer.status}")

            def json(self):
                return outer.payload

        return _Resp()


class ReconcileTests(unittest.TestCase):
    """Aggregate margin reconciliation: portal billed vs upstream spend."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = tempfile.mkdtemp(prefix="aigw-reconcile-")
        self.db_path = os.path.join(self.tmp, "test.db")
        self.stack.enter_context(patch.object(config_module.settings, "DATABASE_PATH", self.db_path))
        init_db()

    def _seed_log(self, billed, observed_upstream, tokens_in=1000, tokens_out=100,
                  cached=0, estimated=False, is_premium=1):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO request_logs "
                "(user_id, model, tokens_used, tokens_in, tokens_out, tokens_cached, "
                " cost_usd, upstream_cost_usd, is_premium, usage_source) "
                "VALUES ('u-hex', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (DEEPSEEK, tokens_in + tokens_out, tokens_in, tokens_out, cached,
                 billed, observed_upstream, is_premium,
                 "estimated" if estimated else "upstream"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_margin_is_billed_minus_upstream_and_gap_is_surfaced(self):
        from app.services import reconcile_service
        # 3 premium rows billed at 0.02 total; upstream account says 0.005.
        self._seed_log(0.008, 0.002)
        self._seed_log(0.007, 0.002, estimated=True)
        self._seed_log(0.005, 0.001)
        # A free row must be excluded from premium reconciliation.
        self._seed_log(9.99, 9.99, is_premium=0)

        _FakeReconcileClient.payload = {
            "rangeTotal": 3, "totalCostUsdc": "0.005000",
            "totalTokens": 3300, "totalSavedUsdc": "1.5",
        }
        with patch.object(reconcile_service.httpx, "AsyncClient", _FakeReconcileClient), \
             patch.object(config_module.settings, "PREMIUM_MANAGEMENT_URL", "https://example.test/api"), \
             patch.object(config_module.settings, "PREMIUM_UPSTREAM_KEY", "sk-test"):
            import asyncio
            data = asyncio.run(reconcile_service.reconcile(days=7))

        self.assertEqual(data["portal"]["requests"], 3)
        self.assertAlmostEqual(data["portal"]["billed_usd"], 0.020, places=9)
        self.assertEqual(data["portal"]["estimated_requests"], 1)
        self.assertAlmostEqual(data["upstream"]["cost_usdc"], 0.005, places=9)
        self.assertAlmostEqual(data["margin"]["gross_usd"], 0.015, places=9)
        self.assertEqual(data["margin"]["ratio"], 4.0)
        # Observed upstream (0.005) equals the account total (0.005): no leak.
        self.assertAlmostEqual(data["margin"]["observed_vs_upstream_gap_usd"], 0.0, places=9)

    def test_upstream_failure_still_returns_portal_side(self):
        from app.services import reconcile_service
        self._seed_log(0.02, 0.004)

        class _Boom(_FakeReconcileClient):
            status = 500

        with patch.object(reconcile_service.httpx, "AsyncClient", _Boom), \
             patch.object(config_module.settings, "PREMIUM_MANAGEMENT_URL", "https://example.test/api"), \
             patch.object(config_module.settings, "PREMIUM_UPSTREAM_KEY", "sk-test"):
            import asyncio
            data = asyncio.run(reconcile_service.reconcile(days=7))

        self.assertIsNone(data["upstream"])
        self.assertIsNotNone(data["upstream_error"])
        self.assertAlmostEqual(data["portal"]["billed_usd"], 0.02, places=9)
        self.assertNotIn("margin", data)

    def test_admin_endpoint_requires_admin_and_returns_data(self):
        from app.main import app
        client = self.stack.enter_context(TestClient(app))

        _FakeReconcileClient.payload = {
            "rangeTotal": 1, "totalCostUsdc": "0.002000", "totalTokens": 100, "totalSavedUsdc": "0",
        }
        with patch.object(config_module.settings, "PREMIUM_MANAGEMENT_URL", "https://example.test/api"), \
             patch.object(config_module.settings, "PREMIUM_UPSTREAM_KEY", "sk-test"), \
             patch("app.services.reconcile_service.httpx.AsyncClient", _FakeReconcileClient):
            with patch("app.routers.pages.get_session_user", return_value={"role": "user"}):
                denied = client.get("/api/admin/reconcile?days=7")
            self.assertEqual(denied.status_code, 403)

            with patch("app.routers.pages.get_session_user", return_value={"role": "admin"}):
                ok = client.get("/api/admin/reconcile?days=7")
            self.assertEqual(ok.status_code, 200)
            body = ok.json()
            self.assertEqual(body["status"], "ok")
            self.assertIn("portal", body["data"])
            self.assertIn("upstream", body["data"])


if __name__ == "__main__":
    unittest.main()
