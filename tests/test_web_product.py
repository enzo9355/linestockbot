import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test")
os.environ.setdefault("LINE_CHANNEL_SECRET", "test")

import app as stock_app


def analysis_data():
    return {
        "name": "台積電", "code": "2330", "price": 100.0, "prob": 63,
        "trend": "多頭", "rsi": 58.0, "ma20": 98.0, "macd_osc": 0.3,
        "k": 62.0, "d": 54.0, "s_score": 55.0, "s_status": "中性",
        "candles": "[]", "ma20_line": "[]", "prob_h": "[]", "pred": "[]",
        "news": [],
        "projection": {
            "ok": True, "amount": 100000, "shares": 1000,
            "deployed_amount": 100000, "strategy_profit": 8000,
            "buy_hold_profit": 5000, "strategy_annualized": 8.0,
            "buy_hold_annualized": 5.0,
        },
        "foreign_flow": {
            "available": True, "net_5": 1500, "net_20": 3200,
            "status": "外資偏多", "source": "外資",
        },
        "bt": {
            "days": 100, "accuracy": 54.0, "brier": 0.23,
            "strat_cum": 8.0, "bh_cum": 5.0, "win_rate": 57.0,
            "trades": 7, "mdd": -6.0, "sharpe": 1.1,
            "conclusion": "風險調整後表現尚可", "top_features": ["成交量", "RSI", "法人"],
        },
    }


class WebProductTests(unittest.TestCase):
    def test_base_shell_uses_stock_papi_brand_and_light_theme(self):
        response = stock_app.app.test_client().get("/dashboard")
        html = response.get_data(as_text=True)
        css = Path(stock_app.app.static_folder, "app.css").read_text(encoding="utf-8")

        self.assertIn("Stock Papi", html)
        self.assertIn("市場首頁", html)
        self.assertIn("Lora", html)
        self.assertIn("GenWanMin", css)
        self.assertIn("--bg:#f6efe6", css)
        self.assertIn(".glass-panel", css)
        self.assertNotIn("量化觀測站", html)

    def test_dashboard_page_is_the_stock_papi_landing_page(self):
        with patch.object(stock_app, "analyze") as analyze:
            response = stock_app.app.test_client().get("/dashboard")

        self.assertEqual(response.status_code, 200)
        analyze.assert_not_called()
        html = response.get_data(as_text=True)
        for label in ["市場摘要", "產業預測", "精選標的", "新手投資小辭典", "LINE 管理關注"]:
            self.assertIn(label, html)
        self.assertNotIn("強勢訊號", html)
        for web_only_removed in ["我的關注", "最近提醒", "data-alert-preview", "/watchlist"]:
            self.assertNotIn(web_only_removed, html)
        self.assertIn('data-dashboard-endpoint="/api/dashboard"', html)
        self.assertIn('data-top-picks', html)
        self.assertIn('data-watchlist-strip', html)

    @patch.object(stock_app, "analyze")
    @patch.object(stock_app, "load_sector_signal_snapshot")
    def test_dashboard_api_returns_sector_cards_and_top_picks(self, load_snapshot, analyze):
        analyze.return_value = {"price": 23150.0, "prob": 58, "trend": "多頭"}
        load_snapshot.return_value = {
            "sectors": {
                "半導體": [{
                    "code": "2330", "name": "台積電", "prob": 72,
                    "trend": "多頭", "score": 91.2, "as_of": "2026-06-28", "foreign_net_5": 12000,
                }],
                "AI 伺服器": [{
                    "code": "6669", "name": "緯穎", "prob": 69,
                    "trend": "多頭", "score": 88.5, "as_of": "2026-06-28", "foreign_net_5": 5400,
                }],
            }
        }
        previous = stock_app._SYSTEM_CACHE.copy()
        self.addCleanup(stock_app._SYSTEM_CACHE.update, previous)
        self.addCleanup(stock_app._SYSTEM_CACHE.clear)
        stock_app._SYSTEM_CACHE.clear()
        now = time.time()
        stock_app._SYSTEM_CACHE.update({
            "2330": ({"code": "2330", "name": "台積電", "prob": 72}, now),
            "2317": ({"code": "2317", "name": "鴻海", "prob": 61}, now),
        })

        response = stock_app.app.test_client().get("/api/dashboard")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["market"]["price"], 23150.0)
        self.assertEqual([item["code"] for item in payload["opportunities"]], ["2330", "2317"])
        self.assertEqual(payload["sector_cards"][0]["name"], "半導體")
        self.assertEqual(payload["sector_cards"][0]["leader"]["code"], "2330")
        self.assertEqual(len(payload["top_picks"]), 2)
        self.assertEqual(payload["watchlist_hint"]["title"], "關注與提醒在 LINE 管理")

    @patch.object(stock_app, "analyze", return_value=analysis_data())
    def test_stock_page_is_the_core_analysis_workspace(self, _analyze):
        response = stock_app.app.test_client().get("/stock/2330")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for label in ["五日上漲機率", "技術指標", "新手解讀", "風險提醒"]:
            self.assertIn(label, html)
        for label in ["投資金額試算", "外資買賣超", "約可買股數", "外資偏多"]:
            self.assertIn(label, html)
        for web_only_removed in ["加入關注", "設定提醒", "data-watchlist-add", "data-alert-open"]:
            self.assertNotIn(web_only_removed, html)
        self.assertIn("data-chart-range", html)
        self.assertIn("<details", html)
        self.assertIn("/static/app.css", html)

    @patch.object(stock_app, "analyze", return_value=analysis_data())
    def test_stock_page_uses_summary_chart_news_first_flow(self, _analyze):
        response = stock_app.app.test_client().get("/stock/2330")
        html = response.get_data(as_text=True)

        for label in ["預測摘要", "價格與預測軌跡", "近期新聞", "新手解讀"]:
            self.assertIn(label, html)
        self.assertIn("glass-segmented", html)
        self.assertIn("chart-shell", html)

    def test_web_is_analysis_only_and_old_watchlist_redirects(self):
        client = stock_app.app.test_client()
        response = client.get("/watchlist")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/dashboard"))

    @patch.object(stock_app, "analyze")
    def test_stock_summary_api_removed_with_browser_watchlist(self, analyze):
        response = stock_app.app.test_client().get("/api/stock/2330/summary")

        self.assertEqual(response.status_code, 404)
        analyze.assert_not_called()

    def test_line_navigation_maps_six_entries_to_web_routes_and_line_actions(self):
        navigation = stock_app.build_line_navigation_flex("https://example.com/")

        self.assertEqual(navigation["type"], "carousel")
        self.assertEqual(len(navigation["contents"]), 6)
        expected_uri = {
            "看大盤": "https://example.com/market",
            "深度分析": "https://example.com/dashboard",
        }
        actual_uri = {}
        actual_message = {}
        for card in navigation["contents"]:
            self.assertEqual(len(card["footer"]["contents"]), 1)
            action = card["footer"]["contents"][0]["action"]
            title = card["body"]["contents"][0]["text"]
            if action["type"] == "uri":
                actual_uri[title] = action["uri"]
            else:
                actual_message[title] = action["text"]
        self.assertEqual(actual_uri, expected_uri)
        self.assertEqual(actual_message, {
            "查自選": "我的關注",
            "找機會": "預測",
            "設提醒": "提醒管理",
            "算報酬": "投資試算",
        })
        self.assertNotIn("強勢訊號", actual_message)

    def test_rich_menu_source_is_plain_text_and_large(self):
        svg = Path("assets/rich-menu.svg").read_text(encoding="utf-8")

        for label in ["看大盤", "找機會", "查自選", "設提醒", "算報酬", "深度分析"]:
            self.assertIn(label, svg)
        for old_label in ["今日盤勢", "我的關注", "產業預測", "提醒管理", "投資試算", "完整分析"]:
            self.assertNotIn(old_label, svg)
        for emoji in ["📈", "⭐", "🏭", "🔔", "🧮", "📊"]:
            self.assertNotIn(emoji, svg)
        for marker in ["STOCK PAPI", "#f6efe6", "#7fd7c4", "#f4b58a", "#b8a6ea"]:
            self.assertIn(marker, svg)
        self.assertIn('font:800 132px', svg)
        self.assertIn('font:700 48px', svg)

    def test_line_summary_card_has_one_clear_cta(self):
        card = stock_app.build_line_summary_card(
            "強勢訊號", ["2330 台積電", "五日上漲機率 68%"],
            "查看完整分析", "https://example.com/stock/2330",
        )

        self.assertEqual(len(card["footer"]["contents"]), 1)
        self.assertEqual(
            card["footer"]["contents"][0]["action"]["uri"],
            "https://example.com/stock/2330",
        )

    def test_web_shell_supports_keyboard_and_mobile_interactions(self):
        response = stock_app.app.test_client().get("/dashboard")
        html = response.get_data(as_text=True)
        css = Path(stock_app.app.static_folder, "app.css").read_text(encoding="utf-8")

        for marker in ['class="skip-link"', 'id="main-content"', 'aria-live="polite"']:
            self.assertIn(marker, html)
        for rule in [":focus-visible", "prefers-reduced-motion", "min-height:44px"]:
            self.assertIn(rule, css)

    def test_browser_bundle_has_no_local_watchlist_storage(self):
        source = Path(stock_app.app.static_folder, "app.js").read_text(encoding="utf-8")

        for removed in ["localStorage", "quant-watchlist", "data-alert-open", "data-alert-form"]:
            self.assertNotIn(removed, source)
        self.assertIn("initReturnCalculator", source)

    def test_stock_chart_is_clipped_and_resizes_with_its_panel(self):
        css = Path(stock_app.app.static_folder, "app.css").read_text(encoding="utf-8")
        js = Path(stock_app.app.static_folder, "app.js").read_text(encoding="utf-8")

        self.assertIn(".chart-shell{overflow:hidden", css)
        self.assertIn(".stock-chart{", css)
        self.assertIn("min-height:320px", css)
        self.assertIn("function measureChartHeight", js)
        self.assertIn("Math.min(460", js)
        self.assertIn("ResizeObserver", js)


if __name__ == "__main__":
    unittest.main()
