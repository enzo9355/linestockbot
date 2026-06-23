# Daily Sector Signals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a daily sector prediction snapshot so LINE sector forecasts show recently computed market data without running multi-stock analysis inside the webhook.

**Architecture:** Keep the existing Flask monolith and add small pure functions for sector scoring and snapshot generation. Persist one JSON snapshot document in Firestore through the existing `FirestoreStore` token/session machinery, then make LINE sector selection read that snapshot.

**Tech Stack:** Python 3.10, Flask, Firestore REST, LINE Flex Message dictionaries, existing `analyze()`, `unittest`.

---

## File Structure

- Modify `app.py`
  - Add sector constants and pure snapshot helpers near the existing industry map.
  - Add Firestore JSON snapshot load/save helpers near scheduler code.
  - Add `POST /tasks/refresh-sector-signals`.
  - Change `選產業_<name>` message handling to read snapshots instead of slicing the static industry list.
- Modify `tests/test_line_flow.py`
  - Add pure scoring/snapshot tests.
  - Add scheduler endpoint tests.
  - Add LINE sector selection test proving `analyze()` is not called.
- No new dependency.

---

### Task 1: Sector scoring and snapshot generation

**Files:**
- Modify: `app.py`
- Test: `tests/test_line_flow.py`

- [ ] **Step 1: Write failing tests**

Add tests to `tests/test_line_flow.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
python -m unittest tests.test_line_flow.LineBuilderTests.test_sector_signal_score_rewards_probability_backtest_and_foreign_flow tests.test_line_flow.LineBuilderTests.test_build_sector_signal_snapshot_limits_each_sector_to_twenty_codes -v
```

Expected: fail because `sector_signal_score` and `build_sector_signal_snapshot` do not exist.

- [ ] **Step 3: Implement minimal pure helpers**

Add to `app.py` near `CATEGORY_PAGE_SIZE`:

```python
SECTOR_SCAN_LIMIT = 20
SECTOR_DISPLAY_LIMIT = 10
SECTOR_SNAPSHOT_DOC = "sector_signals"
```

Add near `build_market_map()`:

```python
def _safe_float(value, default=0.0):
    try:
        value = float(value)
        return value if np.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _clamp(value, low, high):
    return max(low, min(high, value))


def sector_signal_score(data):
    bt = data.get("bt") or {}
    foreign = data.get("foreign_flow") or {}
    prob = _safe_float(data.get("prob"))
    strat_bonus = _clamp(_safe_float(bt.get("strat_cum")), -20.0, 20.0) * 0.35
    foreign_bonus = _clamp(_safe_float(foreign.get("net_5")) / 1000.0, -5.0, 5.0)
    drawdown_penalty = min(abs(_safe_float(bt.get("mdd"))), 30.0) * 0.15
    return round(prob + strat_bonus + foreign_bonus - drawdown_penalty, 2)


def sector_candidates(category, codes, limit=SECTOR_SCAN_LIMIT):
    selected = []
    seen = set()
    for code in codes:
        code = str(code).strip()
        if code in seen or not code.isdigit() or len(code) not in (4, 5):
            continue
        if category != "ETF專區" and code.startswith("00"):
            continue
        selected.append(code)
        seen.add(code)
        if len(selected) == limit:
            break
    return selected


def sector_signal_item(code, data):
    if not data or not isinstance(data.get("as_of"), str):
        return None
    bt = data.get("bt") or {}
    foreign = data.get("foreign_flow") or {}
    return {
        "code": code,
        "name": data.get("name") or get_stock_name(code),
        "price": _safe_float(data.get("price")),
        "prob": int(round(_safe_float(data.get("prob")))),
        "trend": data.get("trend") or "中性",
        "score": sector_signal_score(data),
        "strat_cum": _safe_float(bt.get("strat_cum")),
        "mdd": _safe_float(bt.get("mdd")),
        "foreign_net_5": _safe_float(foreign.get("net_5")),
        "as_of": data["as_of"],
    }


def build_sector_signal_snapshot(market_map, analyze_fn, now=None):
    now = now or datetime.datetime.utcnow()
    sectors = {}
    dates = []
    for category, codes in market_map.items():
        items = []
        for code in sector_candidates(category, codes):
            try:
                item = sector_signal_item(code, analyze_fn(code))
            except Exception:
                item = None
            if item:
                items.append(item)
                dates.append(item["as_of"])
        items.sort(key=lambda item: item["score"], reverse=True)
        sectors[category] = items[:SECTOR_DISPLAY_LIMIT]
    return {
        "as_of": max(dates) if dates else now.date().isoformat(),
        "generated_at": now.replace(microsecond=0).isoformat() + "Z",
        "sectors": sectors,
    }
```

- [ ] **Step 4: Run tests to verify pass**

Run the same command. Expected: pass.

- [ ] **Step 5: Commit**

```powershell
git add app.py tests/test_line_flow.py
git commit -m "feat: build daily sector snapshots"
```

---

### Task 2: Firestore persistence and scheduler endpoint

**Files:**
- Modify: `app.py`
- Test: `tests/test_line_flow.py`

- [ ] **Step 1: Write failing endpoint tests**

Add tests to the scheduler endpoint test class in `tests/test_line_flow.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
python -m unittest tests.test_line_flow.ScheduledAlertRouteTests.test_refresh_sector_signals_requires_valid_token tests.test_line_flow.ScheduledAlertRouteTests.test_refresh_sector_signals_runs_after_valid_auth -v
```

Expected: fail because `/tasks/refresh-sector-signals` does not exist.

- [ ] **Step 3: Implement persistence and endpoint**

Add to `app.py` near scheduler helpers:

```python
def _system_document_url(store, document_id):
    return (
        "https://firestore.googleapis.com/v1/projects/"
        f"{store.project_id}/databases/(default)/documents/system/{urllib.parse.quote(document_id, safe='')}"
    )


def save_sector_signal_snapshot(store, snapshot):
    body = {
        "fields": {
            "payload": {
                "stringValue": json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
            }
        }
    }
    response = store._request(
        "PATCH",
        _system_document_url(store, SECTOR_SNAPSHOT_DOC),
        timeout=10,
        params={"updateMask.fieldPaths": "payload"},
        json=body,
    )
    if response.status_code != 200:
        raise StoreError(f"sector snapshot write failed with status {response.status_code}")


def load_sector_signal_snapshot(store):
    response = store._request("GET", _system_document_url(store, SECTOR_SNAPSHOT_DOC), timeout=5)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise StoreError(f"sector snapshot read failed with status {response.status_code}")
    try:
        raw = response.json().get("fields", {}).get("payload", {}).get("stringValue")
        snapshot = json.loads(raw)
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("sectors"), dict):
            raise ValueError("invalid snapshot")
        return snapshot
    except (TypeError, ValueError, json.JSONDecodeError):
        raise StoreError("sector snapshot response was invalid") from None


def refresh_sector_signals(store):
    snapshot = build_sector_signal_snapshot(industry_map, analyze)
    save_sector_signal_snapshot(store, snapshot)
    return snapshot
```

Add route:

```python
@app.route("/tasks/refresh-sector-signals", methods=["POST"])
def refresh_sector_signals_task():
    if not ALERT_TASK_TOKEN:
        return "產業預測排程尚未設定", 503
    if not hmac.compare_digest(
        request.headers.get("Authorization", ""),
        f"Bearer {ALERT_TASK_TOKEN}",
    ):
        return "身份驗證失敗", 403
    if line_store is None:
        return "關注功能尚未設定", 503
    try:
        snapshot = refresh_sector_signals(line_store)
    except Exception:
        return "產業預測排程執行失敗", 500
    return f"產業預測排程執行完成：{snapshot.get('as_of')}", 200
```

- [ ] **Step 4: Run tests to verify pass**

Run the same command. Expected: pass.

- [ ] **Step 5: Commit**

```powershell
git add app.py tests/test_line_flow.py
git commit -m "feat: schedule sector signal refresh"
```

---

### Task 3: LINE reads sector snapshots

**Files:**
- Modify: `app.py`
- Test: `tests/test_line_flow.py`

- [ ] **Step 1: Write failing LINE test**

Add test to `MessageFlowTests`:

```python
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

    with patch.object(stock_app, "line_store", object()), \
         patch.object(stock_app, "load_sector_signal_snapshot", return_value=snapshot), \
         patch.object(stock_app, "analyze") as analyze, \
         patch.object(stock_app, "line_bot_api", line_api):
        stock_app.handle_message(event)

    analyze.assert_not_called()
    message = line_api.reply_message.call_args.args[1]
    self.assertEqual(message.type, "flex")
    self.assertIn("台積電", str(message.contents))
```

- [ ] **Step 2: Run test to verify failure**

Run:

```powershell
python -m unittest tests.test_line_flow.MessageFlowTests.test_sector_selection_uses_snapshot_without_running_analysis -v
```

Expected: fail because sector selection still uses the old code list carousel.

- [ ] **Step 3: Implement snapshot carousel**

Add to `app.py` near `build_industry_carousel()`:

```python
def _build_sector_signal_row(item):
    code = item["code"]
    name = item["name"]
    return {
        "type": "box",
        "layout": "vertical",
        "paddingAll": "12px",
        "cornerRadius": "8px",
        "backgroundColor": "#ffffff",
        "spacing": "xs",
        "margin": "md",
        "action": {"type": "message", "label": f"查詢 {code}", "text": code},
        "contents": [
            {"type": "text", "text": f"{name} ({code})", "color": "#0f172a", "size": "md", "weight": "bold", "wrap": True},
            {"type": "text", "text": f"AI勝率 {item['prob']}%｜{item['trend']}｜外資5日 {item['foreign_net_5']:,.0f}", "color": "#475569", "size": "xs", "wrap": True},
            {"type": "text", "text": f"排序分數 {item['score']:.1f}｜資料 {item['as_of']}", "color": "#0284c7", "size": "xs", "wrap": True},
        ],
    }


def build_sector_signal_carousel(category, items):
    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#0f766e",
            "paddingAll": "16px",
            "contents": [{"type": "text", "text": f"📊 {category}｜每日產業預測", "color": "#ffffff", "weight": "bold", "size": "lg", "wrap": True}],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#f8fafc",
            "paddingAll": "12px",
            "contents": [_build_sector_signal_row(item) for item in items[:SECTOR_DISPLAY_LIMIT]],
        },
    }
```

Change `選產業_` branch:

```python
elif msg.startswith("選產業_"):
    cat = msg.replace("選產業_", "")
    try:
        snapshot = load_sector_signal_snapshot(line_store) if line_store else None
    except StoreError:
        snapshot = None
    items = (snapshot or {}).get("sectors", {}).get(cat, [])
    if items:
        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(
                alt_text=f"{cat} 每日產業預測",
                contents=build_sector_signal_carousel(cat, items),
            ),
        )
    else:
        _reply_text(event, "產業資料尚未更新，請稍後再試。你也可以直接輸入股票代碼查詢個股。")
```

- [ ] **Step 4: Run test to verify pass**

Run the same command. Expected: pass.

- [ ] **Step 5: Commit**

```powershell
git add app.py tests/test_line_flow.py
git commit -m "feat: show sector snapshots in LINE"
```

---

### Task 4: Full verification and push

**Files:**
- Verify only.

- [ ] **Step 1: Run focused tests**

```powershell
python -m unittest tests.test_line_flow -v
```

Expected: all tests pass.

- [ ] **Step 2: Run broader test suite**

```powershell
python -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 3: Check formatting safety**

```powershell
git diff --check
```

Expected: no output.

- [ ] **Step 4: Push**

```powershell
git push origin main
```

Expected: `main -> main`.
