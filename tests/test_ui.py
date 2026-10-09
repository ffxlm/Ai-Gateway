"""UI smoke tests with isolated fixtures; never write to the portal database."""
import unittest
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.core.catalog import FREE_MODELS, PREMIUM_MODELS
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
            self.assertIn(f'${model["price_in_usd"]} / ${model["price_out_usd"]}', response.text)
        self.assertIn('/auth/discord/login', response.text)
        self.assertIn('from</span> openai', response.text)
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
            ("GLM-5.3-Flash", "ZHIPU AI", "zhipu"),
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
        self.assertIn(">A</span>", str(identity.provider_logo("unknown-model", "Acme", "model-glyph")))

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
        for icon in ["deepseek", "zhipu", "minimax", "openai"]:
            self.assertIn(f'/static/providers/{icon}.svg', response.text)
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
        self.assert_no_redundant_navigation(admin_response.text)
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

    def test_protected_pages_still_redirect_guests(self):
        self.mock("get_session_user", return_value=None)
        for path in ["/", "/admin", "/admin/requests"]:
            self.assertEqual(self.client.get(path, follow_redirects=False).status_code, 307)


if __name__ == "__main__":
    unittest.main()
