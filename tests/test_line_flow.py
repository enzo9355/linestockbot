import copy
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test")
os.environ.setdefault("LINE_CHANNEL_SECRET", "test")

import app as stock_app
from line_state import StoreError, empty_state


def sample_data():
    return {
        "price": 1000.0,
        "prob": 68,
        "trend": "多頭",
        "s_score": 55.0,
        "s_status": "中性",
    }


def postback_event(data, user_id="U123"):
    return SimpleNamespace(
        source=SimpleNamespace(user_id=user_id),
        postback=SimpleNamespace(data=data),
        reply_token="reply",
    )


def message_event(text, user_id="U123"):
    return SimpleNamespace(
        source=SimpleNamespace(user_id=user_id),
        message=SimpleNamespace(text=text),
        reply_token="reply",
    )


class CopyOnWriteStore:
    def __init__(self, state=None):
        self.state = state if state is not None else empty_state()
        self.updated_user_ids = []

    def load(self, user_id):
        return copy.deepcopy(self.state), "v1"

    def update(self, user_id, mutate):
        self.updated_user_ids.append(user_id)
        candidate = copy.deepcopy(self.state)
        mutate(candidate)
        self.state = candidate
        return copy.deepcopy(self.state)


class InterleavingStore(CopyOnWriteStore):
    def __init__(self, observed_state, latest_state):
        super().__init__(latest_state)
        self.observed_state = copy.deepcopy(observed_state)

    def load(self, user_id):
        observed = self.observed_state
        self.observed_state = copy.deepcopy(self.state)
        return copy.deepcopy(observed), "v1"


class SlowReadStore:
    def __init__(self, delay=0.75):
        self.delay = delay
        self.update_calls = 0

    def load(self, user_id):
        time.sleep(self.delay)
        return empty_state(), "v1"

    def update(self, user_id, mutate):
        self.update_calls += 1
        raise AssertionError("read timeout must never schedule a late update")


class BlockingReadStore:
    def __init__(self):
        self.release_reads = threading.Event()
        self.condition = threading.Condition()
        self.load_calls = 0
        self.finished_calls = 0

    def load(self, user_id):
        with self.condition:
            self.load_calls += 1
            self.condition.notify_all()
        self.release_reads.wait(timeout=2)
        with self.condition:
            self.finished_calls += 1
            self.condition.notify_all()
        return empty_state(), "v1"

    def wait_for(self, attribute, expected, timeout=1):
        deadline = time.monotonic() + timeout
        with self.condition:
            while getattr(self, attribute) < expected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
        return True


class LineBuilderTests(unittest.TestCase):
    def test_stock_flex_has_two_postbacks_and_one_analysis_uri(self):
        card = stock_app.build_stock_flex_message(
            "2330", "台積電", sample_data(),
            "https://example.com/stock/2330", watched=False,
        )

        actions = [item["action"] for item in card["footer"]["contents"]]

        self.assertEqual([action["type"] for action in actions], ["postback", "postback", "uri"])
        self.assertEqual(actions[0]["data"], "watch:add:2330")
        self.assertEqual(actions[1]["data"], "alert:menu:2330")
        self.assertEqual(actions[2], {
            "type": "uri", "label": "查看完整分析",
            "uri": "https://example.com/stock/2330",
        })

    def test_stock_flex_supports_watched_and_legacy_four_argument_callers(self):
        legacy = stock_app.build_stock_flex_message(
            "2330", "台積電", sample_data(), "https://example.com/stock/2330",
        )
        watched = stock_app.build_stock_flex_message(
            "2330", "台積電", sample_data(), "https://example.com/stock/2330", watched=True,
        )

        self.assertEqual(legacy["footer"]["contents"][0]["action"]["data"], "watch:add:2330")
        self.assertEqual(watched["footer"]["contents"][0]["action"]["data"], "watch:remove:2330")

    def test_watchlist_empty_is_one_explanatory_bubble(self):
        message = stock_app.build_watchlist_flex(empty_state(), "https://example.com")

        self.assertEqual(message["type"], "bubble")
        self.assertIn("尚未", str(message))
        self.assertNotIn("/watchlist", str(message))

    def test_watchlist_limits_twelve_and_merges_matching_snapshot(self):
        state = empty_state()
        state["watchlist"] = [
            {"code": str(1000 + index), "name": f"股票{index}", "added_at": index}
            for index in range(13)
        ]
        state["signals"] = {
            "as_of": "2026-06-22",
            "items": [{
                "code": "1000", "name": "股票0", "price": 50.5,
                "prob": 66, "trend": "多頭", "as_of": "2026-06-21",
            }],
        }

        message = stock_app.build_watchlist_flex(state, "https://example.com/")

        self.assertEqual(message["type"], "carousel")
        self.assertEqual(len(message["contents"]), 12)
        first, second = message["contents"][:2]
        self.assertIn("50.50", str(first))
        self.assertIn("66%", str(first))
        self.assertIn("2026-06-21", str(first))
        self.assertIn("待收盤更新", str(second))
        actions = [item["action"] for item in first["footer"]["contents"]]
        self.assertEqual([action["type"] for action in actions], ["postback", "postback", "uri"])
        self.assertEqual(actions[0]["data"], "watch:remove:1000")
        self.assertEqual(actions[1]["data"], "alert:menu:1000")
        self.assertEqual(actions[2]["uri"], "https://example.com/stock/1000")
        self.assertNotIn("/watchlist", str(message))

    def test_alert_menu_has_four_strict_postbacks(self):
        card = stock_app.build_alert_menu_flex("2330", "台積電")
        actions = [item["action"] for item in card["body"]["contents"] if item["type"] == "button"]

        self.assertEqual([action["data"] for action in actions], [
            "alert:start:2330:price",
            "alert:start:2330:probability",
            "alert:trend:2330:多頭",
            "alert:trend:2330:空頭",
        ])
        self.assertTrue(all(action["type"] == "postback" for action in actions))

    def test_strong_signals_empty_is_one_explanatory_bubble(self):
        message = stock_app.build_strong_signals_flex(empty_state(), "https://example.com")

        self.assertEqual(message["type"], "bubble")
        self.assertIn("尚無", str(message))

    @patch.object(stock_app, "analyze")
    def test_strong_signals_preserves_snapshot_order_and_does_not_analyze(self, analyze):
        state = empty_state()
        state["signals"] = {
            "as_of": "2026-06-22",
            "items": [
                {"code": "2317", "name": "鴻海", "price": 210.0, "prob": 60, "trend": "多頭", "as_of": "2026-06-21"},
                {"code": "2330", "name": "台積電", "price": 1000.0, "prob": 75, "trend": "多頭", "as_of": "2026-06-22"},
            ],
        }

        message = stock_app.build_strong_signals_flex(state, "https://example.com")

        analyze.assert_not_called()
        self.assertEqual(len(message["contents"]), 2)
        self.assertIn("2317", str(message["contents"][0]))
        self.assertIn("2026-06-21", str(message["contents"][0]))
        self.assertIn("2330", str(message["contents"][1]))
        for code, card in zip(("2317", "2330"), message["contents"]):
            self.assertEqual(len(card["footer"]["contents"]), 1)
            self.assertEqual(card["footer"]["contents"][0]["action"]["uri"], f"https://example.com/stock/{code}")


class PostbackTests(unittest.TestCase):
    def call(self, payload, state=None, search_result=("2330", "台積電")):
        store = CopyOnWriteStore(state)
        line_api = Mock()
        with patch.object(stock_app, "line_store", store), \
             patch.object(stock_app, "line_bot_api", line_api), \
             patch.object(stock_app, "search_stock_code", return_value=search_result):
            stock_app.handle_postback(postback_event(payload))
        line_api.reply_message.assert_called_once()
        return store, line_api

    def test_watch_add_and_remove_update_current_user(self):
        store, _ = self.call("watch:add:2330")
        self.assertEqual(store.updated_user_ids, ["U123"])
        self.assertEqual(store.state["watchlist"][0]["code"], "2330")

        store, _ = self.call("watch:remove:2330", store.state)
        self.assertEqual(store.state["watchlist"], [])

    def test_alert_start_adds_watch_and_pending(self):
        store, _ = self.call("alert:start:2330:probability")

        self.assertEqual(store.state["watchlist"][0]["code"], "2330")
        self.assertEqual(store.state["pending"]["kind"], "probability")

    def test_alert_trend_adds_watch_and_alert(self):
        store, _ = self.call("alert:trend:2330:空頭")

        self.assertEqual(store.state["watchlist"][0]["code"], "2330")
        self.assertEqual((store.state["alerts"][0]["kind"], store.state["alerts"][0]["value"]), ("trend", "空頭"))

    def test_repeated_trend_postback_is_idempotent(self):
        store, _ = self.call("alert:trend:2330:多頭")
        store, line_api = self.call("alert:trend:2330:多頭", store.state)

        self.assertEqual(len(store.state["alerts"]), 1)
        self.assertIn("已存在", line_api.reply_message.call_args.args[1].text)

    def test_alert_menu_replies_without_updating(self):
        store, line_api = self.call("alert:menu:2330")

        self.assertEqual(store.updated_user_ids, [])
        reply = line_api.reply_message.call_args.args[1]
        self.assertEqual(reply.type, "flex")

    def test_alert_remove_filters_exact_hex_id_and_handles_missing(self):
        state = empty_state()
        alert_id = "a" * 32
        state["alerts"] = [{
            "id": alert_id, "code": "2330", "name": "台積電",
            "kind": "price", "value": 900, "enabled": True,
            "last_triggered_date": None,
        }]
        store, _ = self.call(f"alert:remove:{alert_id}", state)
        self.assertEqual(store.state["alerts"], [])

        store, line_api = self.call(f"alert:remove:{'b' * 32}", store.state)
        self.assertIn("找不到", line_api.reply_message.call_args.args[1].text)

    def test_invalid_payloads_and_unknown_codes_are_rejected_without_update(self):
        invalid = [
            "watch:add:2330:extra", "watch:delete:2330",
            "alert:start:2330:trend", "alert:trend:2330:盤整",
            "alert:remove:not-hex", "unknown",
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                store, line_api = self.call(payload)
                self.assertEqual(store.updated_user_ids, [])
                self.assertIn("無效", line_api.reply_message.call_args.args[1].text)

        store, line_api = self.call("watch:add:9999", search_result=(None, None))
        self.assertEqual(store.updated_user_ids, [])
        self.assertIn("找不到", line_api.reply_message.call_args.args[1].text)

    def test_requires_line_user_id(self):
        line_api = Mock()
        with patch.object(stock_app, "line_store", CopyOnWriteStore()), patch.object(stock_app, "line_bot_api", line_api):
            stock_app.handle_postback(postback_event("watch:add:2330", user_id=None))

        line_api.reply_message.assert_called_once()
        self.assertIn("無法識別", line_api.reply_message.call_args.args[1].text)

    def test_missing_store_and_store_error_return_safe_messages(self):
        line_api = Mock()
        with patch.object(stock_app, "line_store", None), patch.object(stock_app, "line_bot_api", line_api):
            stock_app.handle_postback(postback_event("watch:add:2330"))
        self.assertIn("關注功能尚未設定", line_api.reply_message.call_args.args[1].text)

        failing = Mock()
        failing.update.side_effect = StoreError("internal raw response U123 token")
        line_api.reset_mock()
        with patch.object(stock_app, "line_store", failing), \
             patch.object(stock_app, "line_bot_api", line_api), \
             patch.object(stock_app, "search_stock_code", return_value=("2330", "台積電")):
            stock_app.handle_postback(postback_event("watch:add:2330"))
        text = line_api.reply_message.call_args.args[1].text
        self.assertIn("稍後再試", text)
        self.assertNotIn("U123", text)
        self.assertNotIn("token", text)


class MessageFlowTests(unittest.TestCase):
    def call(self, text, store, analyze_result=None):
        line_api = Mock()
        with stock_app.app.test_request_context("/callback", base_url="https://example.com/"), \
             patch.object(stock_app, "line_store", store), \
             patch.object(stock_app, "line_bot_api", line_api), \
             patch.object(stock_app, "analyze", return_value=analyze_result), \
             patch.object(stock_app, "search_stock_code", return_value=(None, None)):
            stock_app.handle_message(message_event(text))
        line_api.reply_message.assert_called_once()
        return line_api

    def test_pending_numeric_success_creates_alert_and_stops_stock_lookup(self):
        state = empty_state()
        state["pending"] = {"code": "2330", "name": "台積電", "kind": "probability", "expires_at": 9999999999}
        store = CopyOnWriteStore(state)
        line_api = Mock()
        with stock_app.app.test_request_context("/callback", base_url="https://example.com/"), \
             patch.object(stock_app, "line_store", store), \
             patch.object(stock_app, "line_bot_api", line_api), \
             patch.object(stock_app, "search_stock_code") as search:
            stock_app.handle_message(message_event("65"))

        search.assert_not_called()
        self.assertIsNone(store.state["pending"])
        self.assertEqual((store.state["alerts"][0]["kind"], store.state["alerts"][0]["value"]), ("probability", 65.0))
        line_api.reply_message.assert_called_once()
        self.assertIn("已建立", line_api.reply_message.call_args.args[1].text)

    def test_pending_cancel_clears_pending_without_alert(self):
        state = empty_state()
        state["pending"] = {"code": "2330", "name": "台積電", "kind": "price", "expires_at": 9999999999}
        store = CopyOnWriteStore(state)

        line_api = self.call("取消", store)

        self.assertIsNone(store.state["pending"])
        self.assertEqual(store.state["alerts"], [])
        self.assertIn("已取消", line_api.reply_message.call_args.args[1].text)

    def test_pending_invalid_input_replies_and_preserves_pending(self):
        state = empty_state()
        pending = {"code": "2330", "name": "台積電", "kind": "price", "expires_at": 9999999999}
        state["pending"] = pending.copy()
        store = CopyOnWriteStore(state)

        line_api = self.call("不是數字", store)

        self.assertEqual(store.state["pending"], pending)
        self.assertIn("有效數字", line_api.reply_message.call_args.args[1].text)

    def test_expired_pending_is_cleared_and_persisted_by_copy_on_write_store(self):
        state = empty_state()
        state["pending"] = {
            "code": "2330", "name": "台積電", "kind": "price", "expires_at": 1,
        }
        store = CopyOnWriteStore(state)

        line_api = self.call("900", store)

        self.assertIsNone(store.state["pending"])
        self.assertEqual(store.state["alerts"], [])
        self.assertIn("逾時", line_api.reply_message.call_args.args[1].text)

    def test_pending_numeric_rejects_interleaved_replacement(self):
        observed = empty_state()
        observed["pending"] = {
            "code": "2330", "name": "台積電", "kind": "probability", "expires_at": 9999999999,
        }
        latest = empty_state()
        latest["pending"] = {
            "code": "2317", "name": "鴻海", "kind": "price", "expires_at": 9999999998,
        }
        store = InterleavingStore(observed, latest)

        line_api = self.call("65", store)

        self.assertEqual(store.state, latest)
        self.assertIn("已變更", line_api.reply_message.call_args.args[1].text)

    def test_pending_cancel_rejects_interleaved_replacement(self):
        observed = empty_state()
        observed["pending"] = {
            "code": "2330", "name": "台積電", "kind": "probability", "expires_at": 9999999999,
        }
        latest = empty_state()
        latest["pending"] = {
            "code": "2330", "name": "台積電", "kind": "price", "expires_at": 9999999998,
        }
        store = InterleavingStore(observed, latest)

        line_api = self.call("取消", store)

        self.assertEqual(store.state, latest)
        self.assertIn("已變更", line_api.reply_message.call_args.args[1].text)

    def test_repeated_numeric_alert_is_idempotent_and_clears_pending(self):
        state = empty_state()
        state["alerts"] = [{
            "id": "a" * 32, "code": "2330", "name": "台積電",
            "kind": "probability", "value": 65.0, "enabled": True,
            "last_triggered_date": None,
        }]
        state["pending"] = {
            "code": "2330", "name": "台積電", "kind": "probability", "expires_at": 9999999999,
        }
        store = CopyOnWriteStore(state)

        line_api = self.call("65", store)

        self.assertIsNone(store.state["pending"])
        self.assertEqual(len(store.state["alerts"]), 1)
        self.assertIn("已存在", line_api.reply_message.call_args.args[1].text)

    def test_slow_state_reads_fail_open_for_non_state_commands_within_budget(self):
        cases = ("2330", "大盤", "新手教學")
        stores = []
        for text in cases:
            with self.subTest(text=text):
                store = SlowReadStore()
                stores.append(store)
                line_api = Mock()
                started = time.perf_counter()
                with stock_app.app.test_request_context("/callback", base_url="https://example.com/"), \
                     patch.object(stock_app, "line_store", store), \
                     patch.object(stock_app, "line_bot_api", line_api), \
                     patch.object(stock_app, "search_stock_code", return_value=("2330", "台積電")), \
                     patch.object(stock_app, "analyze", return_value=sample_data()):
                    stock_app.handle_message(message_event(text))
                elapsed = time.perf_counter() - started

                self.assertLess(elapsed, 0.55)
                line_api.reply_message.assert_called_once()

        time.sleep(0.8)
        self.assertTrue(all(store.update_calls == 0 for store in stores))

    def test_background_state_readers_are_bounded_and_slots_are_reusable(self):
        store = BlockingReadStore()
        limit = stock_app.LINE_STATE_READ_MAX_WORKERS
        slots = threading.BoundedSemaphore(limit)

        with patch.object(stock_app, "line_store", store), \
             patch.object(stock_app, "_line_state_read_slots", slots):
            for _ in range(limit):
                with self.assertRaises(StoreError):
                    stock_app.get_line_state_bounded("U123", timeout=0.01)
            self.assertTrue(store.wait_for("load_calls", limit))

            started = time.perf_counter()
            with self.assertRaises(StoreError):
                stock_app.get_line_state_bounded("U123", timeout=0.2)
            self.assertLess(time.perf_counter() - started, 0.05)
            self.assertEqual(store.load_calls, limit)

            store.release_reads.set()
            self.assertTrue(store.wait_for("finished_calls", limit))
            for _ in range(limit):
                self.assertTrue(slots.acquire(timeout=0.2))
            for _ in range(limit):
                slots.release()
            state = stock_app.get_line_state_bounded("U123", timeout=0.2)

        self.assertEqual(state, empty_state())
        self.assertEqual(store.load_calls, limit + 1)

    def test_watchlist_and_strong_signals_reply_in_line(self):
        state = empty_state()
        state["watchlist"] = [{"code": "2330", "name": "台積電", "added_at": 1}]
        state["signals"] = {"as_of": "2026-06-22", "items": []}
        store = CopyOnWriteStore(state)

        for text in ("我的關注", "強勢訊號"):
            with self.subTest(text=text):
                line_api = self.call(text, store)
                contents = line_api.reply_message.call_args.args[1].contents
                self.assertNotIn("/watchlist", str(contents))

    def test_missing_or_failing_store_returns_safe_message_for_native_commands(self):
        for text in ("我的關注", "強勢訊號"):
            with self.subTest(text=text, failure="missing"):
                line_api = self.call(text, None)
                self.assertIn("關注功能尚未設定", line_api.reply_message.call_args.args[1].text)

            failing = Mock()
            failing.load.side_effect = StoreError("raw response token U123")
            with self.subTest(text=text, failure="store"):
                line_api = self.call(text, failing)
                reply = line_api.reply_message.call_args.args[1].text
                self.assertIn("稍後再試", reply)
                self.assertNotIn("token", reply)

    def test_stock_query_uses_watched_state_but_survives_store_error(self):
        data = sample_data()
        state = empty_state()
        state["watchlist"] = [{"code": "2330", "name": "台積電", "added_at": 1}]
        for store, expected in ((CopyOnWriteStore(state), "watch:remove:2330"), (Mock(), "watch:add:2330")):
            if isinstance(store, Mock):
                store.load.side_effect = StoreError("unavailable")
            line_api = Mock()
            with stock_app.app.test_request_context("/callback", base_url="https://example.com/"), \
                 patch.object(stock_app, "line_store", store), \
                 patch.object(stock_app, "line_bot_api", line_api), \
                 patch.object(stock_app, "search_stock_code", return_value=("2330", "台積電")), \
                 patch.object(stock_app, "analyze", return_value=data):
                stock_app.handle_message(message_event("2330"))
            contents = line_api.reply_message.call_args.args[1].contents
            if hasattr(contents, "as_json_dict"):
                contents = contents.as_json_dict()
            action = contents["footer"]["contents"][0]["action"]
            self.assertEqual(action["data"], expected)


if __name__ == "__main__":
    unittest.main()
