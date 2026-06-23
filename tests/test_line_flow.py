import copy
import json
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
        "bt": {"strat_cum": 8.0, "bh_cum": 5.0, "days": 252},
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


def flex_text(message):
    return json.dumps(message.as_json_dict(), ensure_ascii=False)


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


class ReaderAbort(BaseException):
    pass


class ThreadSetupAbort(BaseException):
    pass


class BaseExceptionReadStore:
    def __init__(self):
        self.condition = threading.Condition()
        self.load_calls = 0

    def load(self, user_id):
        with self.condition:
            self.load_calls += 1
            self.condition.notify_all()
        raise ReaderAbort("simulated reader abort")

    def wait_for_calls(self, expected, timeout=0.2):
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.load_calls < expected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
        return True


class SchedulerStore:
    def __init__(self, users, before_update=None):
        self.users = copy.deepcopy(users)
        self.before_update = before_update
        self.iter_calls = 0
        self.update_calls = []

    def iter_users(self):
        self.iter_calls += 1
        for user_id, state in self.users.items():
            yield user_id, copy.deepcopy(state), "v1"

    def update(self, user_id, mutate):
        self.update_calls.append(user_id)
        if self.before_update:
            self.before_update(self.users[user_id])
            self.before_update = None
        mutate(self.users[user_id])
        return copy.deepcopy(self.users[user_id])


def scheduler_state(code="2330", name="台積電"):
    state = empty_state()
    state["watchlist"] = [{"code": code, "name": name, "added_at": 1}]
    return state


def scheduler_quote(code="2330", name="台積電", **changes):
    quote = {
        "code": code, "name": name, "price": 1000.0,
        "prob": 70, "trend": "多頭", "as_of": "2026-06-22",
    }
    quote.update(changes)
    return quote


class LineBuilderTests(unittest.TestCase):
    def test_sector_signal_score_rewards_probability_backtest_and_foreign_flow(self):
        strong = scheduler_quote(prob=70)
        strong["bt"] = {"strat_cum": 12.0, "mdd": -5.0}
        strong["foreign_flow"] = {"net_5": 3000.0}
        weak = scheduler_quote(prob=55)
        weak["bt"] = {"strat_cum": -8.0, "mdd": -20.0}
        weak["foreign_flow"] = {"net_5": -3000.0}

        self.assertGreater(
            stock_app.sector_signal_score(strong),
            stock_app.sector_signal_score(weak),
        )

    def test_build_sector_signal_snapshot_limits_each_sector_to_twenty_codes(self):
        calls = []

        def analyze(code):
            calls.append(code)
            quote = scheduler_quote(code=code, name=f"股票{code}", prob=50 + int(code[-1]))
            quote["bt"] = {"strat_cum": 1.0, "mdd": -2.0}
            quote["foreign_flow"] = {"net_5": 100.0}
            return quote

        market = {"測試產業": [f"12{i:02d}" for i in range(25)]}
        snapshot = stock_app.build_sector_signal_snapshot(
            market,
            analyze,
            now=stock_app.datetime.datetime(2026, 6, 24, 8, 30),
        )

        self.assertEqual(len(calls), 20)
        self.assertEqual(len(snapshot["sectors"]["測試產業"]), 10)
        self.assertEqual(snapshot["generated_at"], "2026-06-24T08:30:00Z")

    def test_stock_flex_has_three_postbacks_and_one_analysis_uri(self):
        card = stock_app.build_stock_flex_message(
            "2330", "台積電", sample_data(),
            "https://example.com/stock/2330", watched=False,
        )

        actions = [item["action"] for item in card["footer"]["contents"]]

        self.assertEqual([action["type"] for action in actions], ["postback", "postback", "postback", "uri"])
        self.assertEqual(actions[0]["data"], "watch:add:2330")
        self.assertEqual(actions[1]["data"], "alert:menu:2330")
        self.assertEqual(actions[2]["data"], "calc:menu:2330")
        self.assertEqual(actions[3], {
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

    def test_alert_menu_uses_explicit_closing_price_directions(self):
        card = stock_app.build_alert_menu_flex("2330", "台積電")
        actions = [item["action"] for item in card["body"]["contents"] if item["type"] == "button"]

        self.assertEqual([action["data"] for action in actions], [
            "alert:start:2330:price_above",
            "alert:start:2330:price_below",
            "alert:start:2330:probability",
            "alert:trend:2330:多頭",
            "alert:trend:2330:空頭",
        ])
        self.assertTrue(all(action["type"] == "postback" for action in actions))
        self.assertEqual(
            [action["label"] for action in actions],
            ["站上收盤價", "跌破收盤價", "AI 勝率門檻", "趨勢為多頭", "趨勢為空頭"],
        )
        self.assertNotIn("趨勢轉", str(card))

    def test_alert_management_flex_lists_cancel_buttons(self):
        state = empty_state()
        alert_id = "a" * 32
        state["alerts"] = [{
            "id": alert_id, "code": "2330", "name": "台積電",
            "kind": "price", "value": 900, "enabled": True,
            "last_triggered_date": None,
        }]

        message = stock_app.build_alerts_flex(state)
        action = message["contents"][0]["footer"]["contents"][0]["action"]

        self.assertIn("提醒管理", str(message))
        self.assertIn("收盤價站上 900", str(message))
        self.assertEqual(action, {
            "type": "postback",
            "label": "取消提醒",
            "data": f"alert:remove:{alert_id}",
        })

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
        with stock_app.app.test_request_context("/callback", base_url="https://example.com/"), \
             patch.object(stock_app, "line_store", store), \
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
        store, _ = self.call("alert:start:2330:price_below")

        self.assertEqual(store.state["watchlist"][0]["code"], "2330")
        self.assertEqual(store.state["pending"]["kind"], "price_below")

    def test_alert_trend_adds_watch_and_alert(self):
        store, line_api = self.call("alert:trend:2330:空頭")

        self.assertEqual(store.state["watchlist"][0]["code"], "2330")
        self.assertEqual((store.state["alerts"][0]["kind"], store.state["alerts"][0]["value"]), ("trend", "空頭"))
        reply = line_api.reply_message.call_args.args[1].text
        self.assertIn("趨勢為空頭", reply)
        self.assertNotIn("轉為", reply)

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

    def test_calculator_menu_replies_with_preset_amount_buttons(self):
        store, line_api = self.call("calc:menu:2330")

        self.assertEqual(store.updated_user_ids, [])
        reply = line_api.reply_message.call_args.args[1]
        self.assertEqual(reply.type, "flex")
        payload = flex_text(reply)
        self.assertIn("1 萬", payload)
        self.assertIn("calc:amount:2330:100000", payload)

    @patch.object(stock_app, "analyze")
    def test_calculator_amount_postback_replies_with_projection(self, analyze):
        analyze.return_value = {
            "code": "2330", "name": "台積電", "price": 100.0,
            "bt": {"strat_cum": 8.0, "bh_cum": 5.0, "days": 252},
        }

        store, line_api = self.call("calc:amount:2330:100000")

        self.assertEqual(store.updated_user_ids, [])
        self.assertIn("約可買", flex_text(line_api.reply_message.call_args.args[1]))

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

    def test_sector_selection_uses_snapshot_without_running_analysis(self):
        snapshot = {
            "as_of": "2026-06-24",
            "generated_at": "2026-06-24T08:30:00Z",
            "sectors": {
                "半導體": [{
                    "code": "2330", "name": "台積電", "price": 1000.0,
                    "prob": 68, "trend": "多頭", "score": 72.5,
                    "strat_cum": 8.0, "mdd": -6.0,
                    "foreign_net_5": 2000.0, "as_of": "2026-06-24",
                }]
            },
        }
        line_api = Mock()
        event = message_event("選產業_半導體")

        with stock_app.app.test_request_context("/callback", base_url="https://example.com/"), \
             patch.object(stock_app, "line_store", object()), \
             patch.object(stock_app, "load_sector_signal_snapshot", return_value=snapshot), \
             patch.object(stock_app, "analyze") as analyze, \
             patch.object(stock_app, "line_bot_api", line_api):
            stock_app.handle_message(event)

        analyze.assert_not_called()
        message = line_api.reply_message.call_args.args[1]
        self.assertEqual(message.type, "flex")
        self.assertIn("台積電", flex_text(message))

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

    def test_text_calculator_command_replies_with_projection(self):
        data = {
            "code": "2330", "name": "台積電", "price": 100.0,
            "bt": {"strat_cum": 8.0, "bh_cum": 5.0, "days": 252},
        }
        line_api = Mock()
        with stock_app.app.test_request_context("/callback", base_url="https://example.com/"), \
             patch.object(stock_app, "line_store", None), \
             patch.object(stock_app, "line_bot_api", line_api), \
             patch.object(stock_app, "search_stock_code", return_value=("2330", "台積電")), \
             patch.object(stock_app, "analyze", return_value=data):
            stock_app.handle_message(message_event("試算 2330 100000"))

        self.assertIn("約可買", flex_text(line_api.reply_message.call_args.args[1]))

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

    def test_reader_slot_is_released_after_base_exception(self):
        store = BaseExceptionReadStore()
        slots = threading.BoundedSemaphore(1)

        with patch.object(stock_app, "line_store", store), \
             patch.object(stock_app, "_line_state_read_slots", slots):
            with self.assertRaises(StoreError):
                stock_app.get_line_state_bounded("U123", timeout=0.05)
            self.assertTrue(store.wait_for_calls(1))

            with self.assertRaises(StoreError):
                stock_app.get_line_state_bounded("U123", timeout=0.05)
            self.assertTrue(store.wait_for_calls(2))

    def test_reader_slot_is_released_when_thread_start_fails(self):
        slots = threading.BoundedSemaphore(1)
        with patch.object(stock_app, "line_store", CopyOnWriteStore()), \
             patch.object(stock_app, "_line_state_read_slots", slots), \
             patch.object(stock_app.threading, "Thread", side_effect=RuntimeError):
            with self.assertRaises(StoreError):
                stock_app.get_line_state_bounded("U123", timeout=0.05)

        self.assertTrue(slots.acquire(blocking=False))
        slots.release()

    def test_queue_construction_failure_does_not_consume_reader_slot(self):
        slots = threading.BoundedSemaphore(1)
        with patch.object(stock_app, "line_store", CopyOnWriteStore()), \
             patch.object(stock_app, "_line_state_read_slots", slots), \
             patch.object(stock_app.queue, "Queue", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                stock_app.get_line_state_bounded("U123", timeout=0.05)

        self.assertTrue(slots.acquire(blocking=False))
        slots.release()

    def test_thread_setup_base_exception_releases_slot_and_is_reraised(self):
        slots = threading.BoundedSemaphore(1)
        with patch.object(stock_app, "line_store", CopyOnWriteStore()), \
             patch.object(stock_app, "_line_state_read_slots", slots), \
             patch.object(stock_app.threading, "Thread", side_effect=ThreadSetupAbort):
            with self.assertRaises(ThreadSetupAbort):
                stock_app.get_line_state_bounded("U123", timeout=0.05)

        self.assertTrue(slots.acquire(blocking=False))
        slots.release()

    def test_watchlist_and_strong_signals_reply_in_line(self):
        state = empty_state()
        state["watchlist"] = [{"code": "2330", "name": "台積電", "added_at": 1}]
        state["signals"] = {"as_of": "2026-06-22", "items": []}
        store = CopyOnWriteStore(state)

        state["alerts"] = [{
            "id": "a" * 32, "code": "2330", "name": "台積電",
            "kind": "price", "value": 900, "enabled": True,
            "last_triggered_date": None,
        }]

        for text in ("我的關注", "強勢訊號", "提醒管理"):
            with self.subTest(text=text):
                line_api = self.call(text, store)
                contents = line_api.reply_message.call_args.args[1].contents
                self.assertNotIn("/watchlist", str(contents))
                if text == "提醒管理":
                    self.assertIn("alert:remove:" + "a" * 32, str(contents))

    def test_investment_calculator_menu_text_replies_with_usage_hint(self):
        line_api = self.call("投資試算", CopyOnWriteStore())

        reply = line_api.reply_message.call_args.args[1]
        self.assertEqual(reply.type, "flex")
        self.assertIn("試算 2330 100000", flex_text(reply))
        self.assertIn("先輸入股票代碼", flex_text(reply))

    def test_missing_or_failing_store_returns_safe_message_for_native_commands(self):
        for text in ("我的關注", "強勢訊號", "提醒管理"):
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


class ScheduledAlertTests(unittest.TestCase):
    def alert(self, alert_id, kind, value, **changes):
        alert = {
            "id": alert_id, "code": "2330", "name": "台積電",
            "kind": kind, "value": value, "enabled": True,
            "last_triggered_date": None,
        }
        alert.update(changes)
        return alert

    def test_analyzes_each_code_once_and_pushes_personalized_results(self):
        first = scheduler_state()
        first["watchlist"].append({"code": "2317", "name": "鴻海", "added_at": 2})
        first["alerts"] = [self.alert("a" * 32, "price", 900)]
        second = scheduler_state("2317", "鴻海")
        second["alerts"] = [self.alert(
            "b" * 32, "trend", "多頭", code="2317", name="鴻海",
        )]
        store = SchedulerStore({"U1": first, "U2": second})
        quotes = {
            "2330": scheduler_quote(),
            "2317": scheduler_quote("2317", "鴻海", price=210.0, prob=60),
        }
        analyze_calls = []
        pushes = []

        stock_app.run_alert_checks(
            store,
            lambda code: analyze_calls.append(code) or copy.deepcopy(quotes[code]),
            lambda user_id, contents: pushes.append((user_id, contents)),
            "2026-06-23",
            "https://example.com",
        )

        self.assertEqual(store.iter_calls, 1)
        self.assertEqual(sorted(analyze_calls), ["2317", "2330"])
        self.assertEqual(len(analyze_calls), 2)
        self.assertEqual([user_id for user_id, _ in pushes], ["U1", "U2"])
        self.assertEqual(
            [item["code"] for item in store.users["U1"]["signals"]["items"]],
            ["2330", "2317"],
        )
        self.assertEqual(
            [item["code"] for item in store.users["U2"]["signals"]["items"]],
            ["2317"],
        )

    def test_stale_or_missing_as_of_does_not_push_or_update(self):
        stale = scheduler_state()
        stale["signals"] = {
            "as_of": "2026-06-22",
            "items": [scheduler_quote(as_of="2026-06-22")],
        }
        stale["alerts"] = [self.alert("a" * 32, "price", 1)]
        pushes = []

        missing_store = SchedulerStore({"missing": scheduler_state()})
        stock_app.run_alert_checks(
            missing_store,
            lambda code: scheduler_quote(as_of=None),
            lambda *args: pushes.append(args),
            "2026-06-23",
            "https://example.com",
        )
        self.assertEqual(pushes, [])
        self.assertEqual(missing_store.update_calls, [])

        stale_store = SchedulerStore({"stale": stale})
        stock_app.run_alert_checks(
            stale_store,
            lambda code: scheduler_quote(as_of="2026-06-22"),
            lambda *args: pushes.append(args),
            "2026-06-23",
            "https://example.com",
        )
        self.assertEqual(pushes, [])
        self.assertEqual(stale_store.update_calls, [])

    def test_freshness_is_compared_per_stock_code(self):
        state = scheduler_state()
        state["watchlist"].append({"code": "2317", "name": "鴻海", "added_at": 2})
        state["signals"] = {
            "as_of": "2026-06-23",
            "items": [
                scheduler_quote(as_of="2026-06-23"),
                scheduler_quote("2317", "鴻海", price=210.0, prob=60, as_of="2026-06-22"),
            ],
        }
        state["alerts"] = [self.alert(
            "b" * 32, "price", 200, code="2317", name="鴻海",
        )]
        store = SchedulerStore({"U1": state})
        pushes = []

        stock_app.run_alert_checks(
            store,
            lambda code: scheduler_quote(
                code, "鴻海" if code == "2317" else "台積電",
                price=210.0 if code == "2317" else 1000.0,
                prob=60 if code == "2317" else 70,
                as_of="2026-06-23",
            ),
            lambda user_id, contents: pushes.append(contents),
            "2026-06-23", "https://example.com",
        )

        self.assertEqual(len(pushes), 1)
        self.assertIn("2317", str(pushes[0]))
        self.assertNotIn("2330", str(pushes[0]))
        snapshot = {item["code"]: item["as_of"] for item in store.users["U1"]["signals"]["items"]}
        self.assertEqual(snapshot, {"2330": "2026-06-23", "2317": "2026-06-23"})

    def test_analysis_failure_or_none_is_skipped(self):
        for failure in (None, RuntimeError("行情失敗")):
            with self.subTest(failure=failure):
                store = SchedulerStore({"U1": scheduler_state()})
                pushes = []

                def analyze(code):
                    if failure:
                        raise failure
                    return None

                stock_app.run_alert_checks(
                    store, analyze, lambda *args: pushes.append(args),
                    "2026-06-23", "https://example.com",
                )

                self.assertEqual(pushes, [])
                self.assertEqual(store.update_calls, [])

    def test_push_failure_is_isolated_and_raised_without_sensitive_details(self):
        first = scheduler_state()
        first["alerts"] = [self.alert("a" * 32, "price", 1)]
        second = scheduler_state("2317", "鴻海")
        second["alerts"] = [self.alert(
            "b" * 32, "price", 1, code="2317", name="鴻海",
        )]
        store = SchedulerStore({"U-sensitive": first, "U2": second})
        pushed = []

        def push(user_id, contents):
            pushed.append(user_id)
            if user_id == "U-sensitive":
                raise RuntimeError("LINE token secret failed")

        with self.assertRaisesRegex(RuntimeError, "部分 LINE 提醒發送失敗") as raised:
            stock_app.run_alert_checks(
                store,
                lambda code: scheduler_quote(code, "鴻海" if code == "2317" else "台積電"),
                push,
                "2026-06-23",
                "https://example.com",
            )

        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("U-sensitive", str(raised.exception))
        self.assertEqual(pushed, ["U-sensitive", "U2"])
        self.assertEqual(store.update_calls, ["U2"])
        self.assertIsNone(store.users["U-sensitive"]["alerts"][0]["last_triggered_date"])
        self.assertIsNone(store.users["U-sensitive"]["signals"]["as_of"])
        self.assertEqual(store.users["U2"]["alerts"][0]["last_triggered_date"], "2026-06-23")

    def test_alert_kinds_disabled_and_daily_deduplication(self):
        state = scheduler_state()
        state["alerts"] = [
            self.alert("1" * 32, "price", 900),
            self.alert("2" * 32, "probability", 65),
            self.alert("3" * 32, "trend", "多頭"),
            self.alert("4" * 32, "trend", "空頭"),
            self.alert("5" * 32, "price", 1, enabled=False),
            self.alert("6" * 32, "price", 1, last_triggered_date="2026-06-23"),
        ]
        store = SchedulerStore({"U1": state})
        pushes = []

        stock_app.run_alert_checks(
            store, lambda code: scheduler_quote(trend="空頭"),
            lambda user_id, contents: pushes.append(contents),
            "2026-06-23", "https://example.com",
        )

        self.assertEqual(len(pushes), 1)
        self.assertEqual(len(pushes[0]["contents"]), 3)
        marked = {
            alert["id"] for alert in store.users["U1"]["alerts"]
            if alert["last_triggered_date"] == "2026-06-23"
        }
        self.assertEqual(marked, {"1" * 32, "2" * 32, "4" * 32, "6" * 32})

    def test_update_merges_only_scheduler_owned_fields(self):
        observed = scheduler_state()
        observed["alerts"] = [self.alert("a" * 32, "price", 1)]

        def user_changes(latest):
            latest["watchlist"] = [{"code": "2317", "name": "鴻海", "added_at": 2}]
            latest["pending"] = {
                "code": "2317", "name": "鴻海", "kind": "price", "expires_at": 9999999999,
            }
            latest["alerts"].append(self.alert(
                "b" * 32, "price", 200, code="2317", name="鴻海",
            ))

        store = SchedulerStore({"U1": observed}, before_update=user_changes)
        stock_app.run_alert_checks(
            store, lambda code: scheduler_quote(), lambda *args: None,
            "2026-06-23", "https://example.com",
        )

        latest = store.users["U1"]
        self.assertEqual(latest["watchlist"][0]["code"], "2317")
        self.assertEqual(latest["pending"]["code"], "2317")
        self.assertEqual(len(latest["alerts"]), 2)
        self.assertEqual(latest["alerts"][0]["last_triggered_date"], "2026-06-23")
        self.assertIsNone(latest["alerts"][1]["last_triggered_date"])
        self.assertIsNone(latest["signals"]["as_of"])

    def test_push_flex_has_one_web_cta_per_hit(self):
        quote = scheduler_quote()
        hits = [
            {"alert": self.alert("1" * 32, "price", 900), "quote": quote},
            {"alert": self.alert("2" * 32, "trend", "多頭"), "quote": quote},
        ]

        message = stock_app.build_alert_push_flex(hits, "https://example.com/")

        self.assertEqual(message["type"], "carousel")
        self.assertEqual(len(message["contents"]), 2)
        for bubble in message["contents"]:
            buttons = bubble["footer"]["contents"]
            self.assertEqual(len(buttons), 1)
            self.assertEqual(buttons[0]["action"]["type"], "uri")
            self.assertEqual(buttons[0]["action"]["uri"], "https://example.com/stock/2330")
            self.assertIn("條件", str(bubble))
            self.assertNotIn("轉為", str(bubble))

    def test_more_than_twelve_hits_use_one_push_with_legal_carousels(self):
        state = scheduler_state()
        state["alerts"] = [
            self.alert(f"{index:032x}", "price", 1)
            for index in range(13)
        ]
        store = SchedulerStore({"U1": state})
        pushes = []

        stock_app.run_alert_checks(
            store, lambda code: scheduler_quote(),
            lambda user_id, contents: pushes.append(contents),
            "2026-06-23", "https://example.com",
        )

        self.assertEqual(len(pushes), 1)
        self.assertIsInstance(pushes[0], list)
        self.assertEqual([len(message["contents"]) for message in pushes[0]], [12, 1])
        self.assertLessEqual(len(pushes[0]), 5)
        self.assertTrue(all(
            alert["last_triggered_date"] == "2026-06-23"
            for alert in store.users["U1"]["alerts"]
        ))

    def test_users_are_streamed_and_analysis_cache_is_shared(self):
        states = {"U1": scheduler_state(), "U2": scheduler_state()}
        events = []

        class StreamingStore(SchedulerStore):
            def iter_users(self):
                self.iter_calls += 1
                yield "U1", copy.deepcopy(self.users["U1"]), "v1"
                events.append("second-user")
                yield "U2", copy.deepcopy(self.users["U2"]), "v1"

            def update(self, user_id, mutate):
                events.append(f"update:{user_id}")
                return super().update(user_id, mutate)

        store = StreamingStore(states)
        analyze_calls = []
        stock_app.run_alert_checks(
            store,
            lambda code: analyze_calls.append(code) or scheduler_quote(),
            lambda *args: None,
            "2026-06-23", "https://example.com",
        )

        self.assertLess(events.index("update:U1"), events.index("second-user"))
        self.assertEqual(analyze_calls, ["2330"])

    def test_store_update_errors_are_not_swallowed(self):
        store = SchedulerStore({"U1": scheduler_state()})
        store.update = Mock(side_effect=StoreError("firestore unavailable"))

        with self.assertRaisesRegex(StoreError, "firestore unavailable"):
            stock_app.run_alert_checks(
                store, lambda code: scheduler_quote(), lambda *args: None,
                "2026-06-23", "https://example.com",
            )


class ScheduledAlertRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = stock_app.app.test_client()

    def test_refresh_sector_signals_requires_valid_token(self):
        with patch.object(stock_app, "ALERT_TASK_TOKEN", "secret"), \
             patch.object(stock_app, "refresh_sector_signals") as refresh:
            response = self.client.post("/tasks/refresh-sector-signals")

        self.assertEqual(response.status_code, 403)
        refresh.assert_not_called()

    def test_refresh_sector_signals_runs_after_valid_auth(self):
        with patch.object(stock_app, "ALERT_TASK_TOKEN", "secret"), \
             patch.object(stock_app, "line_store", object()), \
             patch.object(stock_app, "refresh_sector_signals", return_value={"as_of": "2026-06-24"}):
            response = self.client.post(
                "/tasks/refresh-sector-signals",
                headers={"Authorization": "Bearer secret"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("2026-06-24", response.get_data(as_text=True))

    def test_missing_token_configuration_returns_503(self):
        with patch.object(stock_app, "ALERT_TASK_TOKEN", None), \
             patch.object(stock_app, "run_alert_checks") as run:
            response = self.client.post("/tasks/check-alerts")
        self.assertEqual(response.status_code, 503)
        run.assert_not_called()

    def test_authorization_requires_exact_bearer_token(self):
        invalid = (None, "", "secret", "Bearer", "Bearer secret extra", "bearer secret")
        for value in invalid:
            headers = {} if value is None else {"Authorization": value}
            with self.subTest(value=value), \
                 patch.object(stock_app, "ALERT_TASK_TOKEN", "secret"), \
                 patch.object(stock_app, "line_store", object()), \
                 patch.object(stock_app, "run_alert_checks") as run:
                response = self.client.post("/tasks/check-alerts", headers=headers)
            self.assertEqual(response.status_code, 403)
            run.assert_not_called()

    def test_missing_store_returns_503_after_valid_auth(self):
        with patch.object(stock_app, "ALERT_TASK_TOKEN", "secret"), \
             patch.object(stock_app, "line_store", None), \
             patch.object(stock_app, "run_alert_checks") as run:
            response = self.client.post(
                "/tasks/check-alerts", headers={"Authorization": "Bearer secret"},
            )
        self.assertEqual(response.status_code, 503)
        run.assert_not_called()

    def test_success_pushes_flex_and_returns_200(self):
        line_api = Mock()

        def run(store, analyze_fn, push_fn, today, base_url):
            push_fn("U1", stock_app.build_alert_push_flex([{
                "alert": {
                    "id": "a" * 32, "code": "2330", "name": "台積電",
                    "kind": "price", "value": 900, "enabled": True,
                    "last_triggered_date": None,
                },
                "quote": scheduler_quote(),
            }], base_url))

        with patch.object(stock_app, "ALERT_TASK_TOKEN", "secret"), \
             patch.object(stock_app, "line_store", object()), \
             patch.object(stock_app, "line_bot_api", line_api), \
             patch.object(stock_app, "run_alert_checks", side_effect=run):
            response = self.client.post(
                "/tasks/check-alerts", headers={"Authorization": "Bearer secret"},
            )

        self.assertEqual(response.status_code, 200)
        line_api.push_message.assert_called_once()
        self.assertEqual(line_api.push_message.call_args.args[0], "U1")
        self.assertEqual(line_api.push_message.call_args.args[1].type, "flex")

    def test_internal_error_does_not_leak_secret(self):
        with patch.object(stock_app, "ALERT_TASK_TOKEN", "secret-value"), \
             patch.object(stock_app, "line_store", object()), \
             patch.object(stock_app, "run_alert_checks", side_effect=RuntimeError("secret-value")):
            response = self.client.post(
                "/tasks/check-alerts", headers={"Authorization": "Bearer secret-value"},
            )
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(b"secret-value", response.data)


class AnalyzeDateTests(unittest.TestCase):
    def test_do_analyze_returns_last_market_date_as_iso(self):
        dates = stock_app.pd.date_range("2025-09-01", periods=200, freq="D", name="Date")
        frame = stock_app.pd.DataFrame({
            "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
            "MA20": 90.0, "AI_P": 70.0, "RSI": 55.0, "Volat": 0.02,
            "MACD_OSC": 1.0, "K": 60.0, "D": 50.0,
        }, index=dates)
        with patch.object(stock_app, "get_data", return_value=frame), \
             patch.object(stock_app, "calc_all", side_effect=lambda value: value), \
             patch.object(stock_app, "run_ai_engine", return_value={"ok": True}), \
             patch.object(stock_app, "get_stock_name", return_value="台積電"), \
             patch.object(stock_app, "get_news", return_value=[]):
            result = stock_app._do_analyze("2330")

        self.assertEqual(result["as_of"], dates[-1].date().isoformat())


if __name__ == "__main__":
    unittest.main()
