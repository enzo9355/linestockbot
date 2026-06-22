import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test")
os.environ.setdefault("LINE_CHANNEL_SECRET", "test")

import app as stock_app


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


if __name__ == "__main__":
    unittest.main()
