"""UI smoke tests with isolated fixtures; never write to the portal database."""
import unittest
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.core.catalog import FREE_MODELS, PREMIUM_MODELS, price_savings
from app.routers.pages import templates


ANALYTICS = {
    "all_time_tokens": 0, "period_tokens": 0, "period_requests": 0,
    "labels": [], "tokens": [], "requests": [], "range": "24h",
}
USER = {
    "id": "ui-test", "username": "Developer", "role": "admin",
    "avatar_url": "/static/brand.svg", "api_key": "sk-portal-example-only",
    "balance": 0, "total_spent": 0, "total_topped_up": 0,
}


class PortalUITests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.mocks = ExitStack()
        self.addCleanup(self.mocks.close)
        self.mock("get_setting", side_effect=lambda key, default=None: default)
        self.mock("premium_trial_limit", side_effect=lambda model: {
            "deepseek-v4.1-flash": 1000000, "gpt-6-sol": 200000, "gpt-6-astra": 50000,
        }[model])

    def mock(self, name, **kwargs):
        return self.mocks.enter_context(patch(f"app.routers.pages.{name}", **kwargs))

    def assert_no_redundant_navigation(self, html):
        for label in ["Gateway home", "console-view-switch", "Preview as user", "Admin view"]:
            self.assertNotIn(label, html)

    def test_landing_displays_catalog_and_local_assets(self):
        self.mock("get_session_user", return_value=None)
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        for model in FREE_MODELS + [m["id"] for m in PREMIUM_MODELS]:
            self.assertIn(model, response.text)
        for model in PREMIUM_MODELS:
            for field in ["price_in_usd", "price_out_usd"]:
                self.assertIn(f'<strong>${model[field]:g}</strong>', response.text)
            for field in ["official_in_usd", "official_out_usd"]:
                self.assertIn(f'<s>${model[field]:g}</s>', response.text)
        self.assertEqual(response.text.count('Save 90%'), len(PREMIUM_MODELS))
        self.assertIn("Reference rates are not verified official prices", response.text)
        self.assertIn('/auth/discord/login', response.text)
        self.assertIn('from</span> openai', response.text)
        self.assertIn("Z.ai", response.text)
        self.assertIn('/static/providers/zai.svg', response.text)
        self.assertNotIn("ZHIPU AI", response.text)
        self.assertNotIn('cdn.tailwindcss.com', response.text)
        for asset in ["brand.svg", "design.css", "site.js", "portal.css", "console.css", "console.js", "product-preview.css"]:
            self.assertEqual(self.client.get(f"/static/{asset}").status_code, 200)

    def test_public_preview_matches_developer_overview(self):
        self.mock("get_session_user", return_value=None)
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        preview = response.text.split('overview-product-stage', 1)[1].split('integration-strip', 1)[0]
        for label in ["AI Gateway", "Personal workspace", "Overview", "Model library",
                      "API credentials", "Usage analytics", "Wallet &amp; billing",
                      "Community &amp; support", "Wallet balance", "Free model access",
                      "Premium spent", "Total deposits", "Usage Analytics", "Product preview", "Sample data"]:
            self.assertIn(label, preview)
        for removed_label in ["Quick search", "Streaming ready", "Gateway home", "Admin workspace"]:
            self.assertNotIn(removed_label, preview)
        self.assertIn('role="img"', preview)
        self.assertIn("226,000 tokens in total", preview)
        self.assertNotIn(USER["api_key"], preview)

    def test_provider_logos_are_local_and_mapped_by_model(self):
        identity = templates.env.get_template("provider_identity.html").module
        for model, provider, icon in [
            ("deepseek-v4-flash", "DeepSeek", "deepseek"),
            ("deepseek-v4.1-flash", "DeepSeek", "deepseek"),
            ("GLM-5.3-Flash", "Z.ai", "zai"),
            ("MiniMax-M2.7", "MiniMax", "minimax"),
            ("gpt-6-sol", "[OI]", "openai"),
            ("gpt-6-astra", "[OI]", "openai"),
        ]:
            with self.subTest(model=model):
                html = str(identity.provider_logo(model, provider, "model-glyph"))
                self.assertIn(f'/static/providers/{icon}.svg', html)
                self.assertIn('alt=""', html)
                response = self.client.get(f"/static/providers/{icon}.svg")
                self.assertEqual(response.status_code, 200)
                svg = ET.fromstring(response.content)
                self.assertEqual(svg.tag, "{http://www.w3.org/2000/svg}svg")
                self.assertTrue(svg.findall("{http://www.w3.org/2000/svg}path"))
        self.assertEqual(str(identity.provider_name("gpt-6-sol", "[OI]")), "OpenAI")
        self.assertEqual(str(identity.provider_name("GLM-5.3-Flash", "ZHIPU AI")), "Z.ai")
        self.assertIn(">A</span>", str(identity.provider_logo("unknown-model", "Acme", "model-glyph")))

    def test_price_savings_are_calculated_and_invalid_references_are_hidden(self):
        self.assertEqual(price_savings(0.015, 0.15), "90")
        self.assertEqual(price_savings(0, 1), "100")
        self.assertEqual(price_savings(1, 3), "66.66")
        for current, reference in [(1, None), (1, 0), (1, 1), (2, 1), (-1, 1), (1, "NaN"), (1, "Infinity")]:
            self.assertIsNone(price_savings(current, reference))
        pricing = templates.env.get_template("model_pricing.html").module
        model = dict(price_in_usd=0.5, official_in_usd=1, price_out_usd=0.25, official_out_usd=1, discount=90)
        html = str(pricing.savings_badge(model))
        self.assertIn("Input: save 50%", html)
        self.assertIn("Output: save 75%", html)
        self.assertNotIn("90%", html)
        self.assertNotIn("<s>", str(pricing.reference_price(1, None)))
        self.assertEqual(str(pricing.savings_badge(dict(price_in_usd=1, price_out_usd=2))).strip(), "")

    def test_login_error_is_visible_and_escaped(self):
        self.mock("get_session_user", return_value=None)
        response = self.client.get("/login?error=discord_denied")
        self.assertIn('role="alert"', response.text)
        self.assertIn("Discord authorization was cancelled.", response.text)
        response = self.client.get("/login?error=%3Cscript%3Ealert(1)%3C/script%3E")
        self.assertNotIn("<script>alert(1)</script>", response.text)

    def test_dashboard_preserves_credentials_and_actions(self):
        self.mock("get_session_user", return_value=USER)
        self.mock("get_user_analytics", return_value=ANALYTICS)
        self.mock("get_wallet_transactions", return_value=[])
        self.mock("premium_trial_status", return_value={
            "remaining": 1000, "limit": 1000, "used": 0, "pct": 0,
        })
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        for value in ["Overview", 'id="api-key-input"', "openTopupModal()", "regenerateKey()",
                      'id="credentials"', 'id="model-library"', 'id="wallet-activity"',
                      'id="usage-analytics"', '/static/portal.css', 'data-console-kind="developer"',
                      'id="console-sidebar"', 'data-console-view="api"',
                      'data-console-view="models"', 'data-console-view="wallet"']:
            self.assertIn(value, response.text)

        self.assert_no_redundant_navigation(response.text)
        for icon in ["deepseek", "zai", "minimax", "openai"]:
            self.assertIn(f'/static/providers/{icon}.svg', response.text)
        self.assertEqual(response.text.count('Save 90%'), len(PREMIUM_MODELS))
        for model in PREMIUM_MODELS:
            for field in ["official_in_usd", "official_out_usd"]:
                self.assertIn(f'<s>${model[field]:g}</s>', response.text)
        self.assertIn("Z.ai", response.text)
        self.assertNotIn("ZHIPU AI", response.text)
        self.assertIn('href="/admin"', response.text)
        self.assertIn('Admin workspace', response.text)
        self.client.cookies.set("portal_view_override", "user")
        preview_response = self.client.get("/")
        self.assert_no_redundant_navigation(preview_response.text)
        self.assertIn('Admin workspace', preview_response.text)

    def test_admin_and_audit_render(self):
        self.mock("get_session_user", return_value=USER)
        self.mock("get_portal_stats", return_value={
            "total_users": 0, "total_balance": 0, "total_spent": 0,
            "total_tokens_today": 0, "requests_today": 0, "all_time_tokens": 0,
        })
        self.mock("get_all_users", return_value=[])
        self.mock("get_recent_payments", return_value=[])
        self.mock("get_all_wallet_transactions", return_value=[])
        self.mock("get_portal_analytics", return_value=ANALYTICS)
        self.mock("get_reconciliation", return_value=[])
        admin_response = self.client.get("/admin")
        self.assertEqual(admin_response.status_code, 200)
        self.assertIn("classList.add('console-booting')", admin_response.text)
        self.assert_no_redundant_navigation(admin_response.text)
        for heading in ["Wallet &amp; top-ups", "Daily free-trial allowances", "Concurrency limits",
                        "API connections &amp; support", "Payment verification"]:
            self.assertIn(heading.replace("&amp;", "&"), admin_response.text)
        for model in PREMIUM_MODELS:
            self.assertIn(f'name="premium_trial_tokens_per_day:{model["id"]}"', admin_response.text)
        self.mock("count_request_logs", return_value=0)
        self.mock("get_request_logs", return_value=[])
        self.mock("get_request_summary", return_value={
            "requests": 0, "tokens_in": 0, "tokens_out": 0, "trial_tokens": 0,
            "cost_usd": 0, "unbilled_usd": 0,
        })
        self.mock("get_trial_history", return_value=[])
        response = self.client.get("/admin/requests")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Request audit", response.text)
        self.assert_no_redundant_navigation(response.text)
        for value in ['id="console-sidebar"', 'id="request-list"', 'id="trial-history"', 'class="console-panel audit-filters"']:
            self.assertIn(value, response.text)

    def test_financial_tables_keep_rows_aligned_and_references_safe(self):
        self.mock("get_session_user", return_value=USER)
        self.mock("get_portal_stats", return_value={
            "total_users": 1, "total_balance": 0, "total_spent": 0,
            "total_tokens_today": 0, "requests_today": 0, "all_time_tokens": 0,
        })
        self.mock("get_all_users", return_value=[])
        self.mock("get_portal_analytics", return_value=ANALYTICS)
        self.mock("get_reconciliation", return_value=[])
        self.mock("get_all_wallet_transactions", return_value=[{
            "username": "Film User", "user_id": "ui-test", "avatar_url": "/static/brand.svg",
            "tx_type": "migration", "amount_usd": 0.2857, "balance_after": 0.2857,
            "description": "Opening balance", "model": None, "created_at": "2026-10-10 20:11:03",
        }])
        self.mock("get_recent_payments", return_value=[{
            "username": "Film User", "user_id": "ui-test", "avatar_url": "/static/brand.svg",
            "amount": 10, "trans_ref": 'bank-ref-1234567890"><script>alert(1)</script>',
            "created_at": "2026-10-10 20:11:03",
        }])
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count('console-financial-table'), 2)
        self.assertEqual(response.text.count('class="console-table-user"'), 2)
        self.assertIn('<span>2026-10-10</span><span>20:11:03</span>', response.text)
        self.assertIn('datetime="2026-10-10T20:11:03"', response.text)
        self.assertIn('data-copy-bank-ref="bank-ref-1234567890&#34;&gt;&lt;script&gt;', response.text)
        self.assertNotIn('<script>alert(1)</script>', response.text)
        self.assertIn('console-money">฿10.00', response.text)
        timestamp = templates.env.get_template("table_timestamp.html").module
        self.assertEqual(str(timestamp.table_timestamp(None)), "—")
        self.assertIn('<span>2026-10-10</span><span>20:11:03</span>', str(timestamp.table_timestamp("2026-10-10T20:11:03")))

    def test_developer_account_has_no_admin_navigation(self):
        self.mock("get_session_user", return_value=dict(USER, role="user"))
        self.mock("get_user_analytics", return_value=ANALYTICS)
        self.mock("get_wallet_transactions", return_value=[])
        self.mock("premium_trial_status", return_value={"remaining": 0, "limit": 0, "used": 0, "pct": 0})
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('href="/admin"', response.text)
        self.assertNotIn('Preview as user', response.text)
        self.assert_no_redundant_navigation(response.text)
        self.assertIn('Wallet & billing', response.text)

    def test_member_names_are_safely_encoded_in_actions(self):
        self.mock("get_session_user", return_value=USER)
        self.mock("get_portal_stats", return_value={
            "total_users": 1, "total_balance": 0, "total_spent": 0,
            "total_tokens_today": 0, "requests_today": 0, "all_time_tokens": 0,
        })
        self.mock("get_all_users", return_value=[dict(USER, username="Alex O'Reilly", today_tokens=0, is_banned=0)])
        self.mock("get_recent_payments", return_value=[])
        self.mock("get_all_wallet_transactions", return_value=[])
        self.mock("get_portal_analytics", return_value=ANALYTICS)
        self.mock("get_reconciliation", return_value=[])
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn(r'Alex O\u0027Reilly', response.text)
        self.assertNotIn("adjustBalance('ui-test', 'Alex O'Reilly')", response.text)

    def test_reload_avoids_prehydration_content_flash(self):
        """Guards the pre-paint gates: the console hides non-active views and the
        landing hides scroll reveals before first paint, so a reload never paints
        every view stacked (console) or fades content out and back (landing)."""
        console_css = self.client.get("/static/console.css").text
        self.assertIn('.console-app.console-booting [data-console-view]', console_css)
        for view in ["overview", "members", "ledger", "reconciliation", "payments", "settings"]:
            self.assertIn(f'[data-active-view="{view}"] [data-console-view~="{view}"]', console_css)
        self.assertIn("classList.remove('console-booting')", self.client.get("/static/console.js").text)

        design_css = self.client.get("/static/design.css").text
        self.assertIn('.reveal-ready .reveal', design_css)
        self.assertNotIn('.will-reveal', design_css)
        self.assertNotIn("classList.add('will-reveal')", self.client.get("/static/site.js").text)

        self.mock("get_session_user", return_value=None)
        landing = self.client.get("/login")
        self.assertIn("classList.add('reveal-ready')", landing.text)
        self.assertIn("classList.remove('reveal-ready')", landing.text)  # fail-open if site.js is missing

        self.mock("get_session_user", return_value=USER)
        self.mock("get_user_analytics", return_value=ANALYTICS)
        self.mock("get_wallet_transactions", return_value=[])
        self.mock("premium_trial_status", return_value={"remaining": 0, "limit": 0, "used": 0, "pct": 0})
        self.assertIn("classList.add('console-booting')", self.client.get("/").text)

    def test_protected_pages_still_redirect_guests(self):
        self.mock("get_session_user", return_value=None)
        for path in ["/", "/admin", "/admin/requests"]:
            self.assertEqual(self.client.get(path, follow_redirects=False).status_code, 307)


if __name__ == "__main__":
    unittest.main()
