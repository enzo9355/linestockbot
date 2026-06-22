import os
import time
import unittest
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
        "bt": {
            "days": 100, "accuracy": 54.0, "brier": 0.23,
            "strat_cum": 8.0, "bh_cum": 5.0, "win_rate": 57.0,
            "trades": 7, "mdd": -6.0, "sharpe": 1.1,
            "conclusion": "風險調整後表現尚可", "top_features": ["成交量", "RSI", "法人"],
        },
    }


class WebProductTests(unittest.TestCase):
    def test_dashboard_page_is_a_fast_decision_shell(self):
        with patch.object(stock_app, "analyze") as analyze:
            response = stock_app.app.test_client().get("/dashboard")

        self.assertEqual(response.status_code, 200)
        analyze.assert_not_called()
        html = response.get_data(as_text=True)
        for label in ["市場摘要", "強勢訊號", "產業雷達", "我的關注", "最近提醒"]:
            self.assertIn(label, html)
        self.assertIn('data-dashboard-endpoint="/api/dashboard"', html)

    @patch.object(stock_app, "analyze")
    def test_dashboard_api_returns_market_and_cached_signals(self, analyze):
        analyze.return_value = {"price": 23150.0, "prob": 58, "trend": "多頭"}
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
        self.assertGreater(len(payload["sectors"]), 3)

    @patch.object(stock_app, "analyze", return_value=analysis_data())
    def test_stock_page_is_the_core_analysis_workspace(self, _analyze):
        response = stock_app.app.test_client().get("/stock/2330")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for label in ["五日上漲機率", "加入關注", "設定提醒", "技術指標", "模型解釋", "風險提醒"]:
            self.assertIn(label, html)
        self.assertIn("data-chart-range", html)
        self.assertIn("<details", html)
        self.assertIn("/static/app.css", html)


if __name__ == "__main__":
    unittest.main()
