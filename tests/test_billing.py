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
)
from app.core.database import init_db
from app.services.user_service import get_today_str

DEEPSEEK = "deepseek-v4.1-flash"  # $0.015 in / $0.06 out per 1M tokens
API_KEY = "sk-portal-test-billing"


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


if __name__ == "__main__":
    unittest.main()
