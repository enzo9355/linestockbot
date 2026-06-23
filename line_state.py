import copy
import json
import math
import re
import time
import uuid
from datetime import date
from urllib.parse import quote, unquote

import requests


MAX_WATCHLIST = 12
MAX_ALERTS = 20
PENDING_SECONDS = 600
METADATA_TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"


class StateError(ValueError):
    pass


class StoreError(RuntimeError):
    pass


class StoreConflict(StoreError):
    pass


class FirestoreStore:
    def __init__(self, project_id, session=None, token_provider=None):
        if not isinstance(project_id, str) or re.fullmatch(
            r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project_id
        ) is None:
            raise ValueError("project_id is invalid")
        self.project_id = project_id
        self.session = session if session is not None else requests.Session()
        self.token_provider = token_provider if token_provider is not None else self._access_token
        self._cached_token = None
        self._token_expires_at = 0

    @property
    def collection_url(self):
        return (
            "https://firestore.googleapis.com/v1/projects/"
            f"{self.project_id}/databases/(default)/documents/line_users"
        )

    def _access_token(self):
        now = time.time()
        if self._cached_token and now < self._token_expires_at:
            return self._cached_token

        try:
            response = self.session.get(
                METADATA_TOKEN_URL,
                headers={"Metadata-Flavor": "Google"},
                timeout=3,
            )
            if response.status_code != 200:
                raise ValueError("metadata status")
            payload = response.json()
            token = payload["access_token"]
            expires_in = payload["expires_in"]
            if isinstance(expires_in, bool):
                raise ValueError("metadata fields")
            expires_in = float(expires_in)
            if (
                not isinstance(token, str)
                or not token
                or not math.isfinite(expires_in)
                or expires_in <= 0
            ):
                raise ValueError("metadata fields")
        except Exception:
            raise StoreError("Metadata token request failed") from None

        self._cached_token = token
        self._token_expires_at = now + max(0, expires_in - 60)
        return token

    def _headers(self):
        try:
            token = self.token_provider()
            if not isinstance(token, str) or not token:
                raise ValueError("invalid token")
        except StoreError:
            raise
        except Exception:
            raise StoreError("Access token provider failed") from None
        return {"Authorization": f"Bearer {token}"}

    def _request(self, method, url, timeout, **kwargs):
        try:
            return self.session.request(
                method,
                url,
                headers=self._headers(),
                timeout=timeout,
                **kwargs,
            )
        except StoreError:
            raise
        except Exception:
            raise StoreError("Firestore request failed") from None

    @staticmethod
    def _document_state(document):
        try:
            raw_state = document.get("fields", {}).get("state", {}).get("stringValue", "{}")
            return normalize_state(json.loads(raw_state))
        except (AttributeError, TypeError, ValueError):
            return empty_state()

    def _document_url(self, user_id):
        return f"{self.collection_url}/{quote(user_id, safe='')}"

    def load(self, user_id):
        response = self._request("GET", self._document_url(user_id), timeout=5)
        if response.status_code == 404:
            return empty_state(), None
        if response.status_code != 200:
            raise StoreError(f"Firestore read failed with status {response.status_code}")
        try:
            document = response.json()
            update_time = document.get("updateTime")
        except Exception:
            raise StoreError("Firestore read response was invalid") from None
        return self._document_state(document), update_time

    def save(self, user_id, state, update_time):
        params = {"updateMask.fieldPaths": "state"}
        if update_time:
            params["currentDocument.updateTime"] = update_time
        else:
            params["currentDocument.exists"] = "false"
        serialized = json.dumps(
            normalize_state(state),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        body = {"fields": {"state": {"stringValue": serialized}}}
        response = self._request(
            "PATCH",
            self._document_url(user_id),
            timeout=5,
            params=params,
            json=body,
        )
        if response.status_code in {409, 412}:
            raise StoreConflict("Firestore write conflict")
        if response.status_code == 400:
            try:
                payload = response.json()
                error = payload.get("error", {})
                if error.get("status") == "FAILED_PRECONDITION":
                    raise StoreConflict("Firestore write conflict")
            except StoreConflict:
                raise
            except Exception:
                pass
        if response.status_code != 200:
            raise StoreError(f"Firestore write failed with status {response.status_code}")
        try:
            update_time = response.json()["updateTime"]
            if not isinstance(update_time, str) or not update_time:
                raise ValueError("invalid updateTime")
            return update_time
        except Exception:
            raise StoreError("Firestore write response was invalid") from None

    def update(self, user_id, mutate):
        for attempt in range(2):
            state, update_time = self.load(user_id)
            mutate(state)
            try:
                self.save(user_id, state, update_time)
                return state
            except StoreConflict:
                if attempt == 1:
                    raise
        raise StoreConflict("Firestore write conflict")

    def iter_users(self):
        page_token = None
        seen_page_tokens = set()
        while True:
            params = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            response = self._request(
                "GET",
                self.collection_url,
                timeout=10,
                params=params,
            )
            if response.status_code != 200:
                raise StoreError(f"Firestore list failed with status {response.status_code}")
            try:
                payload = response.json()
                documents = payload.get("documents", [])
                next_page_token = payload.get("nextPageToken")
                if not isinstance(documents, list):
                    raise ValueError("invalid documents")
                if next_page_token is not None and (
                    not isinstance(next_page_token, str)
                    or not next_page_token
                    or next_page_token in seen_page_tokens
                ):
                    raise ValueError("invalid page token")
            except Exception:
                raise StoreError("Firestore list response was invalid") from None

            if next_page_token is not None:
                seen_page_tokens.add(next_page_token)
            page_token = next_page_token

            for document in documents:
                try:
                    user_id = unquote(document["name"].rsplit("/", 1)[-1])
                    update_time = document.get("updateTime")
                except Exception:
                    raise StoreError("Firestore document was invalid") from None
                yield user_id, self._document_state(document), update_time

            if not page_token:
                return


def _is_valid_code(code):
    return isinstance(code, str) and bool(code) and code.isascii() and code.isalnum()


def _is_nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def _is_valid_stock(code, name):
    return _is_valid_code(code) and _is_nonempty_string(name)


def _validate_stock(code, name):
    if not _is_valid_stock(code, name):
        raise StateError("股票資料格式錯誤")


def _is_finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_valid_alert_value(kind, value):
    if kind in {"price", "price_above", "price_below"}:
        return _is_finite_number(value) and value > 0
    if kind == "probability":
        return _is_finite_number(value) and 1 <= value <= 99
    return kind == "trend" and value in {"多頭", "空頭"}


def _is_iso_date(value):
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _is_optional_iso_date(value):
    return value is None or _is_iso_date(value)


def empty_state():
    return {
        "watchlist": [],
        "alerts": [],
        "pending": None,
        "signals": {"as_of": None, "items": []},
    }


def normalize_state(value):
    state = empty_state()
    if not isinstance(value, dict):
        return state

    watchlist = value.get("watchlist")
    if isinstance(watchlist, list):
        seen_codes = set()
        for item in watchlist:
            if (
                not isinstance(item, dict)
                or not _is_valid_stock(item.get("code"), item.get("name"))
                or not _is_finite_number(item.get("added_at"))
            ):
                continue
            if item["code"] in seen_codes:
                continue
            seen_codes.add(item["code"])
            state["watchlist"].append(
                {
                    "code": item["code"],
                    "name": item["name"],
                    "added_at": copy.deepcopy(item.get("added_at")),
                }
            )
            if len(state["watchlist"]) == MAX_WATCHLIST:
                break

    alerts = value.get("alerts")
    if isinstance(alerts, list):
        for item in alerts:
            if (
                not isinstance(item, dict)
                or not _is_nonempty_string(item.get("id"))
                or not _is_valid_stock(item.get("code"), item.get("name"))
                or item.get("kind") not in {"price", "price_above", "price_below", "probability", "trend"}
                or not _is_valid_alert_value(item["kind"], item.get("value"))
                or not isinstance(item.get("enabled"), bool)
                or not _is_optional_iso_date(item.get("last_triggered_date"))
            ):
                continue
            state["alerts"].append(
                {
                    "id": item["id"],
                    "code": item["code"],
                    "name": item["name"],
                    "kind": item["kind"],
                    "value": copy.deepcopy(item["value"]),
                    "enabled": copy.deepcopy(item.get("enabled", True)),
                    "last_triggered_date": copy.deepcopy(item.get("last_triggered_date")),
                }
            )
            if len(state["alerts"]) == MAX_ALERTS:
                break

    pending = value.get("pending")
    if (
        isinstance(pending, dict)
        and _is_valid_stock(pending.get("code"), pending.get("name"))
        and pending.get("kind") in {"price", "price_above", "price_below", "probability"}
        and _is_finite_number(pending.get("expires_at"))
    ):
        state["pending"] = {
            "code": pending["code"],
            "name": pending["name"],
            "kind": pending["kind"],
            "expires_at": copy.deepcopy(pending["expires_at"]),
        }

    signals = value.get("signals")
    if isinstance(signals, dict):
        items = signals.get("items")
        state["signals"] = {
            "as_of": copy.deepcopy(signals.get("as_of"))
            if _is_optional_iso_date(signals.get("as_of"))
            else None,
            "items": [],
        }
        if isinstance(items, list):
            allowed_fields = ("code", "name", "price", "prob", "trend", "as_of")
            for item in items:
                if (
                    not isinstance(item, dict)
                    or not _is_valid_stock(item.get("code"), item.get("name"))
                    or not _is_finite_number(item.get("price"))
                    or item["price"] <= 0
                    or not _is_finite_number(item.get("prob"))
                    or not 0 <= item["prob"] <= 100
                    or not _is_nonempty_string(item.get("trend"))
                    or not _is_iso_date(item.get("as_of"))
                ):
                    continue
                state["signals"]["items"].append(
                    {
                        field: copy.deepcopy(item[field])
                        for field in allowed_fields
                        if field in item
                    }
                )
                if len(state["signals"]["items"]) == 5:
                    break

    return state


def add_watch(state, code, name, now=None):
    _validate_stock(code, name)
    if any(item.get("code") == code for item in state["watchlist"]):
        return state
    if len(state["watchlist"]) >= MAX_WATCHLIST:
        raise StateError("關注清單最多 12 檔")

    added_at = time.time() if now is None else now
    state["watchlist"].append({"code": code, "name": name, "added_at": added_at})
    return state


def remove_watch(state, code):
    state["watchlist"] = [item for item in state["watchlist"] if item.get("code") != code]
    state["alerts"] = [item for item in state["alerts"] if item.get("code") != code]
    return state


def start_pending(state, code, name, kind, now=None):
    _validate_stock(code, name)
    if kind not in {"price", "price_above", "price_below", "probability"}:
        raise StateError("不支援的提醒類型")

    started_at = time.time() if now is None else now
    state["pending"] = {
        "code": code,
        "name": name,
        "kind": kind,
        "expires_at": started_at + PENDING_SECONDS,
    }
    return state


def add_alert(state, code, name, kind, value):
    _validate_stock(code, name)
    if kind not in {"price", "price_above", "price_below", "probability", "trend"}:
        raise StateError("不支援的提醒類型")
    if not _is_valid_alert_value(kind, value):
        raise StateError("提醒條件格式錯誤")
    if len(state["alerts"]) >= MAX_ALERTS:
        raise StateError("提醒最多 20 條")

    alert = {
        "id": uuid.uuid4().hex,
        "code": code,
        "name": name,
        "kind": kind,
        "value": value,
        "enabled": True,
        "last_triggered_date": None,
    }
    state["alerts"].append(alert)
    return alert


def consume_pending(state, text, now=None):
    pending = state.get("pending")
    current_time = time.time() if now is None else now
    if not pending or pending.get("expires_at", 0) <= current_time:
        state["pending"] = None
        raise StateError("提醒設定已逾時")

    try:
        value = float(text)
    except (TypeError, ValueError) as error:
        raise StateError("請輸入有效數字") from error
    if not math.isfinite(value):
        raise StateError("請輸入有效數字")
    if pending["kind"] in {"price", "price_above", "price_below"} and value <= 0:
        raise StateError("價格必須大於 0")
    if pending["kind"] == "probability" and not 1 <= value <= 99:
        raise StateError("機率必須介於 1 到 99")

    alert = add_alert(
        state,
        pending["code"],
        pending["name"],
        pending["kind"],
        value,
    )
    state["pending"] = None
    return alert


def evaluate_alert(alert, quote):
    if alert["kind"] in {"price", "price_above"}:
        return quote["price"] >= float(alert["value"])
    if alert["kind"] == "price_below":
        return quote["price"] <= float(alert["value"])
    if alert["kind"] == "probability":
        return quote["prob"] >= float(alert["value"])
    return alert["kind"] == "trend" and quote["trend"] == alert["value"]


def top_signals(quotes):
    return sorted(
        (copy.deepcopy(item) for item in quotes),
        key=lambda item: item["prob"],
        reverse=True,
    )[:5]
