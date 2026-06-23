import copy
import json
import unittest
from unittest import mock

from line_state import (
    MAX_ALERTS,
    MAX_WATCHLIST,
    PENDING_SECONDS,
    FirestoreStore,
    StateError,
    StoreConflict,
    StoreError,
    add_alert,
    add_watch,
    consume_pending,
    empty_state,
    evaluate_alert,
    normalize_state,
    remove_watch,
    start_pending,
    top_signals,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class FakeSession:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def patch(self, url, **kwargs):
        return self.request("PATCH", url, **kwargs)


def firestore_document(user_id="user-1", state=None, update_time="2026-06-22T00:00:00Z"):
    if state is None:
        state = empty_state()
    return {
        "name": f"projects/demo/databases/(default)/documents/line_users/{user_id}",
        "fields": {"state": {"stringValue": json.dumps(state)}},
        "updateTime": update_time,
    }


class FirestoreStoreTests(unittest.TestCase):
    def make_store(self, responses=()):
        session = FakeSession(responses)
        return FirestoreStore("demo-project", session=session, token_provider=lambda: "access-token"), session

    def test_constructor_is_network_free_and_rejects_empty_project(self):
        self.assertTrue(issubclass(StoreError, RuntimeError))
        self.assertTrue(issubclass(StoreConflict, StoreError))
        session = FakeSession()

        store = FirestoreStore("demo-project", session=session)

        self.assertEqual(session.calls, [])
        self.assertEqual(
            store.collection_url,
            "https://firestore.googleapis.com/v1/projects/demo-project/databases/(default)/documents/line_users",
        )
        for project_id in (None, "", "   "):
            with self.subTest(project_id=project_id), self.assertRaises(ValueError):
                FirestoreStore(project_id, session=session)

    def test_constructor_accepts_only_standard_project_ids(self):
        self.assertEqual(
            FirestoreStore("line-stock-bot-498908", session=FakeSession()).project_id,
            "line-stock-bot-498908",
        )
        invalid_ids = [
            "short",
            "a" + "b" * 30,
            "Line-stock-bot-498908",
            "line_stock_bot_498908",
            "line-stock-bot-498908/path",
            "line-stock-bot-498908?query=yes",
            "line-stock-bot-498908-",
            " line-stock-bot-498908",
        ]
        for project_id in invalid_ids:
            with self.subTest(project_id=project_id), self.assertRaises(ValueError):
                FirestoreStore(project_id, session=FakeSession())

    @mock.patch("line_state.time.time")
    def test_metadata_token_is_cached_until_refresh_window(self, now):
        now.side_effect = [1000, 1100]
        session = FakeSession(
            [
                FakeResponse(payload={"access_token": "secret-token", "expires_in": 300}),
            ]
        )
        store = FirestoreStore("demo-project", session=session)

        self.assertEqual(store._access_token(), "secret-token")
        self.assertEqual(store._access_token(), "secret-token")
        self.assertEqual(len(session.calls), 1)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(
            url,
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        )
        self.assertEqual(kwargs["headers"], {"Metadata-Flavor": "Google"})
        self.assertEqual(kwargs["timeout"], 3)

    @mock.patch("line_state.time.time")
    def test_metadata_token_refreshes_after_expiry_margin(self, now):
        now.side_effect = [1000, 1241, 1241]
        session = FakeSession(
            [
                FakeResponse(payload={"access_token": "first", "expires_in": 300}),
                FakeResponse(payload={"access_token": "second", "expires_in": 300}),
            ]
        )
        store = FirestoreStore("demo-project", session=session)

        self.assertEqual(store._access_token(), "first")
        self.assertEqual(store._access_token(), "second")
        self.assertEqual(len(session.calls), 2)

    def test_metadata_token_errors_are_sanitized(self):
        bad_responses = [
            RuntimeError("secret transport body"),
            FakeResponse(status_code=500, payload={"secret": "response body"}),
            FakeResponse(payload={"access_token": "visible-secret"}),
            FakeResponse(json_error=ValueError("secret response body")),
        ]
        for response in bad_responses:
            with self.subTest(response=response):
                store = FirestoreStore("demo-project", session=FakeSession([response]))
                with self.assertRaises(StoreError) as raised:
                    store._access_token()
                self.assertNotIn("secret", str(raised.exception).lower())
                self.assertNotIn("body", str(raised.exception).lower())

    def test_metadata_expiry_is_converted_and_must_be_finite_and_positive(self):
        store = FirestoreStore(
            "demo-project",
            session=FakeSession(
                [FakeResponse(payload={"access_token": "token", "expires_in": "300"})]
            ),
        )
        self.assertEqual(store._access_token(), "token")

        invalid_expiries = [True, 0, float("nan"), float("inf"), "NaN", "Infinity"]
        for expires_in in invalid_expiries:
            with self.subTest(expires_in=expires_in):
                store = FirestoreStore(
                    "demo-project",
                    session=FakeSession(
                        [
                            FakeResponse(
                                payload={"access_token": "token", "expires_in": expires_in}
                            )
                        ]
                    ),
                )
                with self.assertRaises(StoreError):
                    store._access_token()

    def test_load_parses_normalizes_and_url_encodes_user_id(self):
        state = {"watchlist": [{"code": "2330", "name": "台積電", "added_at": 1}], "extra": True}
        store, session = self.make_store([FakeResponse(payload=firestore_document(state=state))])

        loaded, version = store.load("user/台灣")

        self.assertEqual(loaded["watchlist"], state["watchlist"])
        self.assertNotIn("extra", loaded)
        self.assertEqual(version, "2026-06-22T00:00:00Z")
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/user%2F%E5%8F%B0%E7%81%A3"), url)
        self.assertEqual(kwargs["timeout"], 5)
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer access-token"})

    def test_load_404_and_malformed_state_return_empty(self):
        store, _ = self.make_store([FakeResponse(status_code=404)])
        self.assertEqual(store.load("missing"), (empty_state(), None))

        document = firestore_document()
        document["fields"]["state"]["stringValue"] = "not json"
        store, _ = self.make_store([FakeResponse(payload=document)])
        self.assertEqual(store.load("bad"), (empty_state(), document["updateTime"]))

    def test_load_non_200_and_transport_errors_raise_store_error(self):
        for response in (FakeResponse(status_code=500, payload={"secret": "body"}), RuntimeError("network secret")):
            with self.subTest(response=response):
                store, _ = self.make_store([response])
                with self.assertRaises(StoreError) as raised:
                    store.load("user")
                self.assertNotIn("secret", str(raised.exception).lower())

    def test_save_with_version_uses_update_precondition_and_normalized_body(self):
        store, session = self.make_store([FakeResponse(payload={"updateTime": "new-version"})])
        state = {"watchlist": [{"code": "2330", "name": "台積電", "added_at": 1, "extra": True}], "extra": True}

        version = store.save("user/id", state, "old-version")

        self.assertEqual(version, "new-version")
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "PATCH")
        self.assertTrue(url.endswith("/user%2Fid"), url)
        self.assertEqual(
            kwargs["params"],
            {"updateMask.fieldPaths": "state", "currentDocument.updateTime": "old-version"},
        )
        self.assertEqual(kwargs["timeout"], 5)
        serialized = kwargs["json"]["fields"]["state"]["stringValue"]
        self.assertNotIn(" ", serialized)
        self.assertEqual(json.loads(serialized), normalize_state(state))
        self.assertEqual(kwargs["json"], {"fields": {"state": {"stringValue": serialized}}})

    def test_save_without_version_requires_document_not_to_exist(self):
        store, session = self.make_store([FakeResponse(payload={"updateTime": "created"})])

        self.assertEqual(store.save("new", empty_state(), None), "created")

        self.assertEqual(
            session.calls[0][2]["params"],
            {"updateMask.fieldPaths": "state", "currentDocument.exists": "false"},
        )

    def test_save_maps_conflicts_and_other_errors(self):
        for status in (409, 412):
            with self.subTest(status=status):
                store, _ = self.make_store([FakeResponse(status_code=status)])
                with self.assertRaises(StoreConflict):
                    store.save("user", empty_state(), "v1")

        for response in (FakeResponse(status_code=500), RuntimeError("network"), FakeResponse(payload={})):
            with self.subTest(response=response):
                store, _ = self.make_store([response])
                with self.assertRaises(StoreError):
                    store.save("user", empty_state(), "v1")

    def test_save_maps_only_failed_precondition_400_to_conflict(self):
        failed_precondition = FakeResponse(
            status_code=400,
            payload={
                "error": {
                    "code": 400,
                    "message": "document changed; secret body",
                    "status": "FAILED_PRECONDITION",
                }
            },
        )
        store, _ = self.make_store([failed_precondition])
        with self.assertRaises(StoreConflict):
            store.save("user", empty_state(), "v1")

        other_bad_request = FakeResponse(
            status_code=400,
            payload={"error": {"status": "INVALID_ARGUMENT", "message": "secret body"}},
        )
        store, _ = self.make_store([other_bad_request])
        with self.assertRaises(StoreError) as raised:
            store.save("user", empty_state(), "v1")
        self.assertNotIn("secret", str(raised.exception).lower())
        self.assertNotIn("body", str(raised.exception).lower())

        malformed_bad_request = FakeResponse(
            status_code=400,
            json_error=ValueError("secret response body"),
        )
        store, _ = self.make_store([malformed_bad_request])
        with self.assertRaises(StoreError) as raised:
            store.save("user", empty_state(), "v1")
        self.assertNotIn("secret", str(raised.exception).lower())
        self.assertNotIn("body", str(raised.exception).lower())

    def test_update_reloads_and_reapplies_mutation_after_first_conflict(self):
        store, _ = self.make_store()
        first = empty_state()
        second = empty_state()
        add_watch(second, "2317", "鴻海", now=1)
        saved = []
        store.load = mock.Mock(side_effect=[(first, "v1"), (second, "v2")])

        def fake_save(user_id, state, version):
            saved.append((copy.deepcopy(state), version))
            if len(saved) == 1:
                raise StoreConflict("conflict")
            return "v3"

        store.save = mock.Mock(side_effect=fake_save)

        result = store.update(
            "user",
            lambda state: add_watch(state, "2330", "台積電", now=2),
        )

        self.assertEqual([item["code"] for item in result["watchlist"]], ["2317", "2330"])
        self.assertEqual(store.load.call_count, 2)

    def test_update_accepts_none_return_and_stops_after_second_conflict(self):
        store, _ = self.make_store()
        store.load = mock.Mock(side_effect=[(empty_state(), "v1"), (empty_state(), "v2")])
        store.save = mock.Mock(side_effect=[StoreConflict("first"), StoreConflict("second")])

        def mutate(state):
            add_watch(state, "2330", "台積電", now=1)

        with self.assertRaises(StoreConflict):
            store.update("user", mutate)
        self.assertEqual(store.load.call_count, 2)
        self.assertEqual(store.save.call_count, 2)

    def test_update_does_not_retry_mutator_errors(self):
        store, _ = self.make_store()
        store.load = mock.Mock(return_value=(empty_state(), "v1"))
        store.save = mock.Mock()

        def fail(_state):
            raise ValueError("bad mutation")

        with self.assertRaisesRegex(ValueError, "bad mutation"):
            store.update("user", fail)
        store.load.assert_called_once_with("user")
        store.save.assert_not_called()

    def test_update_ignores_alert_return_value_and_saves_mutated_state(self):
        state = empty_state()
        add_watch(state, "2330", "台積電", now=1)
        store, _ = self.make_store()
        store.load = mock.Mock(return_value=(state, "v1"))
        store.save = mock.Mock(return_value="v2")

        result = store.update(
            "user",
            lambda current: add_alert(current, "2330", "台積電", "price", 1000),
        )

        saved_state = store.save.call_args.args[1]
        self.assertIn("watchlist", saved_state)
        self.assertEqual(saved_state["watchlist"], state["watchlist"])
        self.assertEqual(len(saved_state["alerts"]), 1)
        self.assertIs(result, state)

    def test_iter_users_paginates_decodes_ids_and_tolerates_bad_state(self):
        first = firestore_document(user_id="user%2Fone")
        bad = firestore_document(user_id="%E5%8F%B0%E7%81%A3")
        bad["fields"]["state"]["stringValue"] = "bad json"
        store, session = self.make_store(
            [
                FakeResponse(payload={"documents": [first], "nextPageToken": "next token"}),
                FakeResponse(payload={"documents": [bad]}),
            ]
        )

        users = list(store.iter_users())

        self.assertEqual(users[0], ("user/one", empty_state(), first["updateTime"]))
        self.assertEqual(users[1], ("台灣", empty_state(), bad["updateTime"]))
        self.assertEqual(session.calls[0][2]["params"], {"pageSize": 100})
        self.assertEqual(session.calls[1][2]["params"], {"pageSize": 100, "pageToken": "next token"})
        self.assertEqual(session.calls[0][2]["timeout"], 10)

    def test_iter_users_allows_empty_pages_and_raises_on_failure(self):
        store, _ = self.make_store([FakeResponse(payload={})])
        self.assertEqual(list(store.iter_users()), [])

        for response in (FakeResponse(status_code=503), RuntimeError("network")):
            with self.subTest(response=response):
                store, _ = self.make_store([response])
                with self.assertRaises(StoreError):
                    list(store.iter_users())

    def test_iter_users_rejects_invalid_or_repeated_page_tokens(self):
        for token in ("", 123, [], {}):
            with self.subTest(token=token):
                store, session = self.make_store(
                    [FakeResponse(payload={"documents": [], "nextPageToken": token})]
                )
                with self.assertRaises(StoreError):
                    list(store.iter_users())
                self.assertEqual(len(session.calls), 1)

        store, session = self.make_store(
            [
                FakeResponse(payload={"documents": [], "nextPageToken": "repeat"}),
                FakeResponse(payload={"documents": [], "nextPageToken": "repeat"}),
            ]
        )
        with self.assertRaises(StoreError) as raised:
            list(store.iter_users())
        self.assertEqual(len(session.calls), 2)
        self.assertNotIn("repeat", str(raised.exception))


class LineStateTests(unittest.TestCase):
    def test_empty_state_and_limits_are_stable(self):
        self.assertEqual(MAX_WATCHLIST, 12)
        self.assertEqual(MAX_ALERTS, 20)
        self.assertEqual(PENDING_SECONDS, 600)
        self.assertEqual(
            empty_state(),
            {
                "watchlist": [],
                "alerts": [],
                "pending": None,
                "signals": {"as_of": None, "items": []},
            },
        )

    def test_watchlist_is_unique_and_limited_to_twelve(self):
        state = empty_state()
        for number in range(MAX_WATCHLIST):
            add_watch(state, str(1000 + number), f"股票{number}", now=1)

        add_watch(state, "1000", "股票0", now=2)

        self.assertEqual(len(state["watchlist"]), MAX_WATCHLIST)
        self.assertEqual(state["watchlist"][0]["added_at"], 1)
        with self.assertRaises(StateError):
            add_watch(state, "9999", "第十三檔", now=3)

    def test_add_watch_rejects_invalid_stock_data(self):
        for code, name in [("", "台積電"), ("23-30", "台積電"), (2330, "台積電"), ("2330", " ")]:
            with self.subTest(code=code, name=name), self.assertRaises(StateError):
                add_watch(empty_state(), code, name)

    def test_stock_codes_must_use_ascii_letters_and_digits(self):
        invalid_codes = ["台積電", "２３３０"]
        for code in invalid_codes:
            with self.subTest(operation="add_watch", code=code), self.assertRaises(StateError):
                add_watch(empty_state(), code, "台積電")

        state = normalize_state(
            {
                "watchlist": [
                    {"code": code, "name": "台積電"}
                    for code in invalid_codes
                ]
            }
        )
        self.assertEqual(state["watchlist"], [])

    def test_remove_watch_also_removes_matching_alerts(self):
        state = empty_state()
        add_watch(state, "2330", "台積電", now=1)
        add_watch(state, "2317", "鴻海", now=1)
        add_alert(state, "2330", "台積電", "price", 1000)
        other_alert = add_alert(state, "2317", "鴻海", "trend", "多頭")

        result = remove_watch(state, "2330")

        self.assertIs(result, state)
        self.assertEqual([item["code"] for item in state["watchlist"]], ["2317"])
        self.assertEqual(state["alerts"], [other_alert])

    def test_pending_success_creates_alert_and_clears_pending(self):
        state = empty_state()

        start_pending(state, "2330", "台積電", "probability", now=100)
        alert = consume_pending(state, "65", now=101)

        self.assertEqual((alert["code"], alert["kind"], alert["value"]), ("2330", "probability", 65.0))
        self.assertIsNone(state["pending"])
        self.assertEqual(state["alerts"], [alert])

    def test_pending_expires_and_rejects_invalid_values(self):
        state = empty_state()
        with self.assertRaises(StateError):
            consume_pending(state, "65", now=1)

        start_pending(state, "2330", "台積電", "price", now=100)
        with self.assertRaises(StateError):
            consume_pending(state, "900", now=701)
        self.assertIsNone(state["pending"])

        invalid_cases = [
            ("price", "not-a-number"),
            ("price", "0"),
            ("probability", "0"),
            ("probability", "100"),
        ]
        for kind, text in invalid_cases:
            with self.subTest(kind=kind, text=text):
                start_pending(state, "2330", "台積電", kind, now=100)
                with self.assertRaises(StateError):
                    consume_pending(state, text, now=101)

        with self.assertRaises(StateError):
            start_pending(state, "2330", "台積電", "trend", now=100)

    def test_start_pending_rejects_invalid_stock_data(self):
        invalid_stocks = [("", "台積電"), ("台積電", "台積電"), ("２３３０", "台積電"), ("2330", " ")]
        for code, name in invalid_stocks:
            with self.subTest(code=code, name=name), self.assertRaises(StateError):
                start_pending(empty_state(), code, name, "price", now=100)

    def test_alerts_are_limited_and_trend_values_are_validated(self):
        state = empty_state()
        for number in range(MAX_ALERTS):
            alert = add_alert(state, "2330", "台積電", "price", number + 1)
            self.assertEqual(len(alert["id"]), 32)
            self.assertTrue(alert["enabled"])
            self.assertIsNone(alert["last_triggered_date"])

        with self.assertRaises(StateError):
            add_alert(state, "2330", "台積電", "price", 21)
        with self.assertRaises(StateError):
            add_alert(empty_state(), "2330", "台積電", "trend", "盤整")
        with self.assertRaises(StateError):
            add_alert(empty_state(), "2330", "台積電", "volume", 10)

    def test_add_alert_rejects_invalid_stock_data_and_numeric_values(self):
        invalid_stocks = [("", "台積電"), ("台積電", "台積電"), ("２３３０", "台積電"), ("2330", " ")]
        for code, name in invalid_stocks:
            with self.subTest(code=code, name=name), self.assertRaises(StateError):
                add_alert(empty_state(), code, name, "price", 100)

        invalid_values = {
            "price": [True, False, 0, -1, float("nan"), float("inf"), float("-inf"), "100"],
            "probability": [True, False, 0, 100, float("nan"), float("inf"), float("-inf"), "65"],
        }
        for kind, values in invalid_values.items():
            for value in values:
                with self.subTest(kind=kind, value=value), self.assertRaises(StateError):
                    add_alert(empty_state(), "2330", "台積電", kind, value)

    def test_evaluate_alert_supports_price_probability_and_trend(self):
        quote = {"code": "2330", "price": 1000.0, "prob": 68, "trend": "多頭"}

        self.assertTrue(evaluate_alert({"kind": "price", "value": 990}, quote))
        self.assertFalse(evaluate_alert({"kind": "price", "value": 1001}, quote))
        self.assertTrue(evaluate_alert({"kind": "price_above", "value": 990}, quote))
        self.assertFalse(evaluate_alert({"kind": "price_above", "value": 1001}, quote))
        self.assertTrue(evaluate_alert({"kind": "price_below", "value": 1001}, quote))
        self.assertFalse(evaluate_alert({"kind": "price_below", "value": 990}, quote))
        self.assertTrue(evaluate_alert({"kind": "probability", "value": 65}, quote))
        self.assertFalse(evaluate_alert({"kind": "probability", "value": 69}, quote))
        self.assertTrue(evaluate_alert({"kind": "trend", "value": "多頭"}, quote))
        self.assertFalse(evaluate_alert({"kind": "trend", "value": "空頭"}, quote))

    def test_top_signals_sorts_copies_and_limits_results(self):
        quotes = [{"code": str(number), "prob": number, "meta": {"rank": number}} for number in range(7)]
        original = copy.deepcopy(quotes)

        signals = top_signals(quotes)
        signals[0]["meta"]["rank"] = -1

        self.assertEqual([item["prob"] for item in signals], [6, 5, 4, 3, 2])
        self.assertEqual(quotes, original)

    def test_normalize_state_drops_malformed_and_unknown_values(self):
        watchlist = [
            {"code": "2330", "name": "台積電", "added_at": 1},
            {"code": "", "name": "空代碼"},
            {"code": "23-17", "name": "非法代碼"},
            {"code": "2317", "name": " "},
            "bad",
        ] + [
            {"code": str(3000 + number), "name": f"股票{number}", "added_at": number + 2}
            for number in range(20)
        ]
        alerts = [
            {
                "id": "a1",
                "code": "2330",
                "name": "台積電",
                "kind": "price",
                "value": 1000,
                "enabled": True,
                "last_triggered_date": None,
            },
            {"id": "a2", "code": "2330", "name": "台積電", "kind": "unknown", "value": 1},
            {"id": 3, "code": "2330", "name": "台積電", "kind": "trend", "value": "多頭"},
        ] + [
            {
                "id": f"alert-{number}",
                "code": "2330",
                "name": "台積電",
                "kind": "probability",
                "value": number,
                "enabled": True,
                "last_triggered_date": None,
            }
            for number in range(1, 26)
        ]
        value = {
            "watchlist": watchlist,
            "alerts": alerts,
            "pending": {
                "code": "2330",
                "name": "台積電",
                "kind": "price",
                "expires_at": 700,
            },
            "signals": {
                "as_of": "2026-06-22",
                "items": [
                    {
                        "code": str(number),
                        "name": f"股票{number}",
                        "price": number + 1,
                        "prob": number,
                        "trend": "多頭",
                        "as_of": "2026-06-22",
                    }
                    for number in range(8)
                ],
                "extra": True,
            },
            "extra": True,
        }

        state = normalize_state(value)

        self.assertEqual(len(state["watchlist"]), MAX_WATCHLIST)
        self.assertEqual(state["watchlist"][0]["code"], "2330")
        self.assertEqual(len(state["alerts"]), MAX_ALERTS)
        self.assertEqual(state["alerts"][0]["id"], "a1")
        self.assertEqual(
            state["pending"],
            {"code": "2330", "name": "台積電", "kind": "price", "expires_at": 700},
        )
        self.assertEqual(
            state["signals"],
            {
                "as_of": "2026-06-22",
                "items": [
                    {
                        "code": str(number),
                        "name": f"股票{number}",
                        "price": number + 1,
                        "prob": number,
                        "trend": "多頭",
                        "as_of": "2026-06-22",
                    }
                    for number in range(5)
                ],
            },
        )
        self.assertNotIn("extra", state)

    def test_normalize_state_validates_values_deduplicates_and_rebuilds_schema(self):
        value = {
            "watchlist": [
                {"code": "2330", "name": "台積電", "added_at": 1, "extra": True},
                {"code": "2330", "name": "重複資料", "added_at": 2},
            ],
            "alerts": [
                {
                    "id": "a1",
                    "code": "2330",
                    "name": "台積電",
                    "kind": "price",
                    "value": 1000,
                    "enabled": False,
                    "last_triggered_date": "2026-06-22",
                    "extra": True,
                }
            ],
            "pending": {
                "code": "2330",
                "name": "台積電",
                "kind": "probability",
                "expires_at": 700,
                "extra": True,
            },
            "signals": {
                "as_of": "2026-06-22",
                "items": [
                    {
                        "code": "2330",
                        "name": "台積電",
                        "price": 1000,
                        "prob": 65,
                        "trend": "多頭",
                        "as_of": "2026-06-22",
                        "extra": True,
                    }
                ],
            },
        }

        state = normalize_state(value)

        self.assertEqual(
            state["watchlist"],
            [{"code": "2330", "name": "台積電", "added_at": 1}],
        )
        self.assertEqual(
            state["alerts"],
            [
                {
                    "id": "a1",
                    "code": "2330",
                    "name": "台積電",
                    "kind": "price",
                    "value": 1000,
                    "enabled": False,
                    "last_triggered_date": "2026-06-22",
                }
            ],
        )
        self.assertEqual(
            state["pending"],
            {"code": "2330", "name": "台積電", "kind": "probability", "expires_at": 700},
        )
        self.assertEqual(
            state["signals"],
            {
                "as_of": "2026-06-22",
                "items": [
                    {
                        "code": "2330",
                        "name": "台積電",
                        "price": 1000,
                        "prob": 65,
                        "trend": "多頭",
                        "as_of": "2026-06-22",
                    }
                ],
            },
        )

        state["watchlist"][0]["added_at"] = 2
        state["alerts"][0]["last_triggered_date"] = "2026-06-23"
        state["pending"]["name"] = "changed"
        state["signals"]["as_of"] = "2026-06-23"
        state["signals"]["items"][0]["name"] = "changed"
        state["signals"]["items"].append({"code": "2317"})
        self.assertEqual(value["watchlist"][0]["added_at"], 1)
        self.assertEqual(value["alerts"][0]["last_triggered_date"], "2026-06-22")
        self.assertEqual(value["pending"]["name"], "台積電")
        self.assertEqual(value["signals"]["as_of"], "2026-06-22")
        self.assertEqual(value["signals"]["items"][0]["name"], "台積電")
        self.assertEqual(len(value["signals"]["items"]), 1)

    def test_normalize_state_drops_invalid_alerts_pending_and_signals(self):
        invalid_alerts = [
            {"id": "", "code": "2330", "name": "台積電", "kind": "price", "value": 1},
            {"id": "a", "code": "台積電", "name": "台積電", "kind": "price", "value": 1},
            {"id": "a", "code": "2330", "name": " ", "kind": "price", "value": 1},
            {"id": "a", "code": "2330", "name": "台積電", "kind": "price", "value": True},
            {"id": "a", "code": "2330", "name": "台積電", "kind": "price", "value": 0},
            {"id": "a", "code": "2330", "name": "台積電", "kind": "price", "value": float("nan")},
            {"id": "a", "code": "2330", "name": "台積電", "kind": "price", "value": float("inf")},
            {"id": "a", "code": "2330", "name": "台積電", "kind": "probability", "value": 100},
            {"id": "a", "code": "2330", "name": "台積電", "kind": "trend", "value": "盤整"},
        ]
        invalid_signals = [
            "bad",
            {"code": "台積電", "prob": 65},
            {"code": "2330", "prob": True},
            {"code": "2330", "prob": float("nan")},
            {"code": "2330", "prob": float("inf")},
        ]

        state = normalize_state(
            {
                "alerts": invalid_alerts,
                "pending": {"code": "2330", "name": "台積電", "kind": "trend", "expires_at": 700},
                "signals": {"items": invalid_signals},
            }
        )

        self.assertEqual(state["alerts"], [])
        self.assertIsNone(state["pending"])
        self.assertEqual(state["signals"]["items"], [])

        malformed_pending = [
            {"code": "台積電", "name": "台積電", "kind": "price", "expires_at": 700},
            {"code": "2330", "name": " ", "kind": "price", "expires_at": 700},
            {"code": "2330", "name": "台積電", "kind": "price", "expires_at": True},
            {"code": "2330", "name": "台積電", "kind": "price", "expires_at": float("nan")},
            {"code": "2330", "name": "台積電", "kind": "price", "expires_at": float("inf")},
        ]
        for pending in malformed_pending:
            with self.subTest(pending=pending):
                self.assertIsNone(normalize_state({"pending": pending})["pending"])

    def test_normalize_state_requires_strict_scalar_schema(self):
        invalid_watchlist = [
            {"code": "2330", "name": "台積電"},
            {"code": "2330", "name": "台積電", "added_at": True},
            {"code": "2330", "name": "台積電", "added_at": float("nan")},
            {"code": "2330", "name": "台積電", "added_at": float("inf")},
        ]
        invalid_alerts = [
            {
                "id": "a1",
                "code": "2330",
                "name": "台積電",
                "kind": "price",
                "value": 1000,
                "enabled": 1,
                "last_triggered_date": None,
            },
            {
                "id": "a2",
                "code": "2330",
                "name": "台積電",
                "kind": "price",
                "value": 1000,
                "enabled": True,
                "last_triggered_date": "2026-02-30",
            },
            {
                "id": "a3",
                "code": "2330",
                "name": "台積電",
                "kind": "price",
                "value": 1000,
                "enabled": True,
                "last_triggered_date": "20260622",
            },
        ]
        invalid_signals = [
            {"code": "2330", "name": "", "price": 1000, "prob": 65, "trend": "多頭", "as_of": "2026-06-22"},
            {"code": "2330", "name": "台積電", "price": True, "prob": 65, "trend": "多頭", "as_of": "2026-06-22"},
            {"code": "2330", "name": "台積電", "price": 0, "prob": 65, "trend": "多頭", "as_of": "2026-06-22"},
            {"code": "2330", "name": "台積電", "price": float("inf"), "prob": 65, "trend": "多頭", "as_of": "2026-06-22"},
            {"code": "2330", "name": "台積電", "price": 1000, "prob": True, "trend": "多頭", "as_of": "2026-06-22"},
            {"code": "2330", "name": "台積電", "price": 1000, "prob": -1, "trend": "多頭", "as_of": "2026-06-22"},
            {"code": "2330", "name": "台積電", "price": 1000, "prob": 101, "trend": "多頭", "as_of": "2026-06-22"},
            {"code": "2330", "name": "台積電", "price": 1000, "prob": 65, "trend": "", "as_of": "2026-06-22"},
            {"code": "2330", "name": "台積電", "price": 1000, "prob": 65, "trend": "多頭", "as_of": "2026-02-30"},
        ]

        state = normalize_state(
            {
                "watchlist": invalid_watchlist,
                "alerts": invalid_alerts,
                "signals": {"as_of": "2026-02-30", "items": invalid_signals},
            }
        )

        self.assertEqual(state["watchlist"], [])
        self.assertEqual(state["alerts"], [])
        self.assertEqual(state["signals"], {"as_of": None, "items": []})

    def test_normalize_state_handles_non_mapping_input(self):
        for value in [None, [], "bad", 1]:
            with self.subTest(value=value):
                self.assertEqual(normalize_state(value), empty_state())


if __name__ == "__main__":
    unittest.main()
