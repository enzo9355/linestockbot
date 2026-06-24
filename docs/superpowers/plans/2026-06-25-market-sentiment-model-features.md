# Market Sentiment Model Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add yfinance-based market context and data-quality features, and structure lightweight news sentiment output without adding heavy dependencies or webhook-only historical leakage.

**Architecture:** Keep `app.py` as the existing single-file Flask/model module. Add small pure helpers for yfinance history, market feature merging, price-quality comparison, and structured sentiment; then thread their outputs into `get_data()`, `calc_all()`, UI data, and existing tests.

**Tech Stack:** Python 3.10, Flask, pandas, numpy, yfinance, LightGBM, unittest.

---

## File Structure

- Modify `app.py`
  - Add market/data-quality constants next to `MODEL_FEATURES`.
  - Add yfinance helper functions near data-fetching helpers.
  - Extend `get_data()` to add market context and price quality columns.
  - Extend `calc_all()` and `MODEL_FEATURES` with market/data-quality features.
  - Replace tuple-only sentiment output with structured dict while preserving current callers.
  - Include data quality and sentiment breakdown in returned analysis data.
- Modify `tests/test_prediction_pipeline.py`
  - Add tests for market feature alignment, yfinance failure fallback, price-quality comparison, and structured sentiment.
- Modify `tests/test_line_flow.py` and `tests/test_web_product.py`
  - Update sample data only if the result payload/UI tests require new fields.

---

### Task 1: Add market feature tests

**Files:**
- Modify: `tests/test_prediction_pipeline.py`
- Modify later: `app.py`

- [ ] **Step 1: Write failing tests for market feature merge**

Add these tests to `PredictionPipelineTests`:

```python
def test_add_market_context_features_aligns_by_date(self):
    dates = pd.to_datetime(["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05", "2026-01-06"])
    stock = pd.DataFrame({
        "Date": dates,
        "Open": [100, 101, 102, 103, 104],
        "High": [101, 102, 103, 104, 105],
        "Low": [99, 100, 101, 102, 103],
        "Close": [100, 102, 101, 103, 106],
        "Volume": [1000, 1100, 1050, 1200, 1300],
    })
    market = pd.DataFrame({
        "Date": dates,
        "Close": [200, 202, 204, 206, 208],
    })
    etf = pd.DataFrame({
        "Date": dates,
        "Close": [50, 51, 52, 51, 53],
    })

    result = stock_app.add_market_context_features(stock, market, etf)

    self.assertIn("MARKET_RET_1", result)
    self.assertIn("ETF50_RET_5", result)
    self.assertIn("STOCK_VS_MARKET_5", result)
    self.assertFalse(result[["MARKET_RET_1", "STOCK_VS_MARKET_5"]].isna().any().any())

def test_add_market_context_features_is_neutral_without_market_data(self):
    dates = pd.to_datetime(["2026-01-02", "2026-01-03"])
    stock = pd.DataFrame({
        "Date": dates,
        "Open": [100, 101],
        "High": [101, 102],
        "Low": [99, 100],
        "Close": [100, 102],
        "Volume": [1000, 1100],
    })

    result = stock_app.add_market_context_features(stock, pd.DataFrame(), pd.DataFrame())

    for column in stock_app.MARKET_FEATURES:
        self.assertIn(column, result)
        self.assertEqual(result[column].tolist(), [0.0, 0.0])
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path '.deps').Path
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_prediction_pipeline.PredictionPipelineTests.test_add_market_context_features_aligns_by_date tests.test_prediction_pipeline.PredictionPipelineTests.test_add_market_context_features_is_neutral_without_market_data -v
```

Expected: fail because `add_market_context_features` or `MARKET_FEATURES` does not exist.

- [ ] **Step 3: Implement minimal market feature helpers**

In `app.py`, add constants:

```python
MARKET_FEATURES = [
    "MARKET_RET_1", "MARKET_RET_5", "MARKET_RET_20", "MARKET_VOL_20",
    "ETF50_RET_5", "STOCK_VS_MARKET_5",
]
DATA_QUALITY_FEATURES = ["DATA_PRICE_DIFF_PCT", "DATA_PRICE_WARNING"]
```

Add helper:

```python
def _neutral_market_features(frame):
    result = frame.copy()
    for column in MARKET_FEATURES:
        result[column] = 0.0
    return result

def _market_feature_frame(market, prefix):
    if market is None or market.empty or "Date" not in market or "Close" not in market:
        return pd.DataFrame()
    frame = market[["Date", "Close"]].copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    close = pd.to_numeric(frame["Close"], errors="coerce")
    frame[f"{prefix}_RET_1"] = close.pct_change(fill_method=None)
    frame[f"{prefix}_RET_5"] = close.pct_change(5, fill_method=None)
    frame[f"{prefix}_RET_20"] = close.pct_change(20, fill_method=None)
    frame[f"{prefix}_VOL_20"] = close.pct_change(fill_method=None).rolling(20).std()
    return frame.drop(columns=["Close"]).dropna(subset=["Date"])

def add_market_context_features(price, market=None, etf50=None):
    if price is None or price.empty:
        return price
    result = price.copy()
    if "Date" not in result:
        return _neutral_market_features(result)
    result["Date"] = pd.to_datetime(result["Date"], errors="coerce")
    market_frame = _market_feature_frame(market, "MARKET")
    if not market_frame.empty:
        result = result.merge(market_frame, on="Date", how="left", suffixes=("", "_MARKET"))
    etf_frame = _market_feature_frame(etf50, "ETF50")
    if not etf_frame.empty:
        result = result.merge(etf_frame[["Date", "ETF50_RET_5"]], on="Date", how="left")
    stock_ret_5 = pd.to_numeric(result["Close"], errors="coerce").pct_change(5, fill_method=None)
    result["STOCK_VS_MARKET_5"] = stock_ret_5 - pd.to_numeric(result.get("MARKET_RET_5", 0), errors="coerce")
    for column in MARKET_FEATURES:
        if column not in result:
            result[column] = 0.0
        result[column] = pd.to_numeric(result[column], errors="coerce").replace([np.inf, -np.inf], 0).fillna(0.0)
    return result
```

- [ ] **Step 4: Run tests and verify pass**

Run the same command as Step 2.

Expected: both tests pass.

---

### Task 2: Add yfinance price-quality tests

**Files:**
- Modify: `tests/test_prediction_pipeline.py`
- Modify later: `app.py`

- [ ] **Step 1: Write failing tests for price-quality comparison**

Add:

```python
def test_add_price_quality_features_flags_large_close_gap(self):
    dates = pd.to_datetime(["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05", "2026-01-06"])
    price = pd.DataFrame({
        "Date": dates,
        "Open": [100, 100, 100, 100, 100],
        "High": [101, 101, 101, 101, 101],
        "Low": [99, 99, 99, 99, 99],
        "Close": [100, 100, 100, 100, 100],
        "Volume": [1000, 1000, 1000, 1000, 1000],
    })
    yf_price = pd.DataFrame({"Date": dates, "Close": [110, 110, 110, 110, 110]})

    result = stock_app.add_price_quality_features(price, yf_price)

    self.assertEqual(result["DATA_PRICE_WARNING"].iloc[-1], 1.0)
    self.assertGreater(result["DATA_PRICE_DIFF_PCT"].iloc[-1], 0.09)
    self.assertEqual(result["YF_CLOSE"].iloc[-1], 110)

def test_add_price_quality_features_is_neutral_without_yfinance(self):
    price = pd.DataFrame({
        "Date": pd.to_datetime(["2026-01-02"]),
        "Open": [100],
        "High": [101],
        "Low": [99],
        "Close": [100],
        "Volume": [1000],
    })

    result = stock_app.add_price_quality_features(price, pd.DataFrame())

    self.assertEqual(result["DATA_PRICE_WARNING"].iloc[-1], 0.0)
    self.assertEqual(result["DATA_PRICE_DIFF_PCT"].iloc[-1], 0.0)
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path '.deps').Path
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_prediction_pipeline.PredictionPipelineTests.test_add_price_quality_features_flags_large_close_gap tests.test_prediction_pipeline.PredictionPipelineTests.test_add_price_quality_features_is_neutral_without_yfinance -v
```

Expected: fail because `add_price_quality_features` does not exist.

- [ ] **Step 3: Implement minimal price-quality helper**

In `app.py`, add:

```python
PRICE_DIFF_WARNING_THRESHOLD = 0.02

def add_price_quality_features(price, yf_price=None):
    result = price.copy()
    result["YF_CLOSE"] = 0.0
    result["DATA_PRICE_DIFF_PCT"] = 0.0
    result["DATA_PRICE_WARNING"] = 0.0
    if yf_price is None or yf_price.empty or "Date" not in yf_price or "Close" not in yf_price:
        return result
    left = result[["Date", "Close"]].copy()
    left["Date"] = pd.to_datetime(left["Date"], errors="coerce")
    right = yf_price[["Date", "Close"]].copy()
    right["Date"] = pd.to_datetime(right["Date"], errors="coerce")
    right = right.rename(columns={"Close": "YF_CLOSE"})
    merged = left.merge(right, on="Date", how="left")
    yf_close = pd.to_numeric(merged["YF_CLOSE"], errors="coerce")
    close = pd.to_numeric(merged["Close"], errors="coerce")
    diff = ((yf_close - close).abs() / (close.abs() + 1e-9)).replace([np.inf, -np.inf], np.nan)
    result["YF_CLOSE"] = yf_close.fillna(0.0).to_numpy()
    result["DATA_PRICE_DIFF_PCT"] = diff.fillna(0.0).to_numpy()
    result["DATA_PRICE_WARNING"] = (result["DATA_PRICE_DIFF_PCT"] > PRICE_DIFF_WARNING_THRESHOLD).astype(float)
    return result
```

- [ ] **Step 4: Run tests and verify pass**

Run the same command as Step 2.

Expected: both tests pass.

---

### Task 3: Wire yfinance helpers into data loading

**Files:**
- Modify: `app.py`
- Modify: `tests/test_prediction_pipeline.py`

- [ ] **Step 1: Write failing integration test**

Add:

```python
@patch("app.fetch_yfinance_price_history")
@patch("app.fetch_finmind_dataset")
def test_get_data_adds_market_and_price_quality_columns(self, finmind, yf_history):
    dates = pd.to_datetime(["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05", "2026-01-06"])
    finmind.side_effect = [
        pd.DataFrame({
            "date": dates.strftime("%Y-%m-%d"),
            "open": [100, 101, 102, 103, 104],
            "max": [101, 102, 103, 104, 105],
            "min": [99, 100, 101, 102, 103],
            "close": [100, 102, 101, 103, 106],
            "Trading_Volume": [1000, 1100, 1050, 1200, 1300],
        }),
        pd.DataFrame(),
        pd.DataFrame(),
    ]
    yf_history.side_effect = [
        pd.DataFrame({"Date": dates, "Open": [100]*5, "High": [101]*5, "Low": [99]*5, "Close": [100, 101, 102, 103, 104], "Volume": [1]*5}),
        pd.DataFrame({"Date": dates, "Open": [200]*5, "High": [201]*5, "Low": [199]*5, "Close": [200, 202, 204, 206, 208], "Volume": [1]*5}),
        pd.DataFrame({"Date": dates, "Open": [50]*5, "High": [51]*5, "Low": [49]*5, "Close": [50, 51, 52, 51, 53], "Volume": [1]*5}),
    ]

    result = stock_app.get_data("2330", days=10)

    for column in stock_app.MARKET_FEATURES + stock_app.DATA_QUALITY_FEATURES:
        self.assertIn(column, result.columns)
    self.assertIn("YF_CLOSE", result.columns)
```

- [ ] **Step 2: Run test and verify failure**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path '.deps').Path
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_prediction_pipeline.PredictionPipelineTests.test_get_data_adds_market_and_price_quality_columns -v
```

Expected: fail because `fetch_yfinance_price_history` is missing or not called.

- [ ] **Step 3: Implement yfinance fetch helper and get_data wiring**

Add:

```python
_YFINANCE_CACHE = {}
YFINANCE_CACHE_SECONDS = 3600

def fetch_yfinance_price_history(tickers, start_date, end_date=None):
    if isinstance(tickers, str):
        tickers = [tickers]
    cache_key = (tuple(tickers), start_date, end_date or "")
    now = time.time()
    cached = _YFINANCE_CACHE.get(cache_key)
    if cached and now - cached[1] < YFINANCE_CACHE_SECONDS:
        return cached[0].copy()
    try:
        import yfinance as yf
        for ticker in tickers:
            hist = yf.download(ticker, start=start_date, end=end_date, progress=False, threads=False)
            if isinstance(hist.columns, pd.MultiIndex):
                hist.columns = hist.columns.droplevel(1)
            if not hist.empty and "Close" in hist.columns:
                frame = hist.copy()
                frame.index = pd.to_datetime(frame.index).tz_localize(None)
                frame.index.name = "Date"
                frame = frame.reset_index()[["Date", "Open", "High", "Low", "Close", "Volume"]]
                _YFINANCE_CACHE[cache_key] = (frame.copy(), now)
                return frame
    except Exception as exc:
        print(f"Yahoo Finance 讀取失敗: {exc}")
    return pd.DataFrame()
```

Update `get_data()`:

```python
yf_price = pd.DataFrame()
if code != "TAIEX":
    yf_price = fetch_yfinance_price_history([f"{code}.TW", f"{code}.TWO"], start_date, end_date)
...
if price is None:
    price = yf_price if code != "TAIEX" else fetch_yfinance_price_history("^TWII", start_date, end_date)
...
price = add_price_quality_features(price, yf_price)
market = fetch_yfinance_price_history("^TWII", start_date, end_date)
etf50 = fetch_yfinance_price_history("0050.TW", start_date, end_date)
price = add_market_context_features(price, market, etf50)
```

- [ ] **Step 4: Run integration test**

Run the same command as Step 2.

Expected: pass.

---

### Task 4: Add model feature and calc_all tests

**Files:**
- Modify: `tests/test_prediction_pipeline.py`
- Modify later: `app.py`

- [ ] **Step 1: Write failing tests for model features**

Add:

```python
def test_model_features_include_market_and_data_quality_features(self):
    for column in stock_app.MARKET_FEATURES + stock_app.DATA_QUALITY_FEATURES:
        self.assertIn(column, stock_app.MODEL_FEATURES)

def test_calc_all_preserves_market_and_data_quality_features(self):
    dates = pd.date_range("2026-01-01", periods=80, freq="D")
    raw = pd.DataFrame({
        "Date": dates,
        "Open": np.linspace(100, 180, len(dates)),
        "High": np.linspace(101, 181, len(dates)),
        "Low": np.linspace(99, 179, len(dates)),
        "Close": np.linspace(100, 180, len(dates)),
        "Volume": np.linspace(1000, 2000, len(dates)),
    })
    for column in stock_app.MARKET_FEATURES + stock_app.DATA_QUALITY_FEATURES:
        raw[column] = 0.1

    result = stock_app.calc_all(raw)

    for column in stock_app.MARKET_FEATURES + stock_app.DATA_QUALITY_FEATURES:
        self.assertIn(column, result.columns)
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path '.deps').Path
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_prediction_pipeline.PredictionPipelineTests.test_model_features_include_market_and_data_quality_features tests.test_prediction_pipeline.PredictionPipelineTests.test_calc_all_preserves_market_and_data_quality_features -v
```

Expected: fail because `MODEL_FEATURES` does not include new features.

- [ ] **Step 3: Update `MODEL_FEATURES` and `calc_all()` neutral columns**

Append `MARKET_FEATURES + DATA_QUALITY_FEATURES` to `MODEL_FEATURES`.

Update `calc_all()` column initialization:

```python
for column in ["Volume", "InstitutionalNet", "ForeignNet", "MarginBalance", "ShortBalance"] + MARKET_FEATURES + DATA_QUALITY_FEATURES:
    if column not in df:
        df[column] = 0.0
    df[column] = pd.to_numeric(df[column], errors="coerce").replace([np.inf, -np.inf], 0).fillna(0.0)
```

- [ ] **Step 4: Run tests and verify pass**

Run the same command as Step 2.

Expected: pass.

---

### Task 5: Structure sentiment output without model leakage

**Files:**
- Modify: `tests/test_prediction_pipeline.py`
- Modify later: `app.py`

- [ ] **Step 1: Write failing tests for structured sentiment**

Add:

```python
def test_analyze_sentiment_returns_breakdown_without_model_side_effects(self):
    news = [
        {"title": "台積電營收創新高 外資看好", "link": "#"},
        {"title": "半導體需求保守 股價下修", "link": "#"},
    ]

    result = stock_app.analyze_sentiment_detail(news)

    self.assertEqual(result["count"], 2)
    self.assertIn("score", result)
    self.assertIn("negative_ratio", result)
    self.assertIn("positive_ratio", result)
    self.assertIn("status", result)

def test_analyze_sentiment_keeps_legacy_tuple_api(self):
    score, status = stock_app.analyze_sentiment([])

    self.assertEqual(score, 50)
    self.assertEqual(status, "中性")
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path '.deps').Path
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_prediction_pipeline.PredictionPipelineTests.test_analyze_sentiment_returns_breakdown_without_model_side_effects tests.test_prediction_pipeline.PredictionPipelineTests.test_analyze_sentiment_keeps_legacy_tuple_api -v
```

Expected: fail because `analyze_sentiment_detail` does not exist.

- [ ] **Step 3: Implement structured sentiment helper**

Replace the existing `analyze_sentiment()` body with:

```python
def analyze_sentiment_detail(news_list):
    if not news_list:
        return {
            "score": 50.0, "status": "中性", "count": 0,
            "positive_ratio": 0.0, "negative_ratio": 0.0,
        }
    pos_words = ["漲", "紅", "高", "多", "買", "利多", "創紀錄", "看好", "強", "優", "雙位數", "營收增", "獲利", "新高", "上揚", "突破"]
    neg_words = ["跌", "綠", "低", "空", "賣", "利空", "虧", "看壞", "弱", "劣", "崩", "違約", "衰退", "下修", "降評", "保守", "跳水"]
    scores = []
    positives = negatives = 0
    for news in news_list:
        title = str(news.get("title", ""))
        score = 0.5
        pos_hits = sum(1 for word in pos_words if word in title)
        neg_hits = sum(1 for word in neg_words if word in title)
        if pos_hits:
            positives += 1
        if neg_hits:
            negatives += 1
        score += 0.15 * pos_hits
        score -= 0.15 * neg_hits
        scores.append(max(0, min(1, score)))
    avg = sum(scores) / len(scores) * 100
    if avg >= 65:
        status = "🔥 樂觀貪婪"
    elif avg <= 35:
        status = "😨 悲觀恐慌"
    else:
        status = "⚖️ 中性觀望"
    return {
        "score": avg,
        "status": status,
        "count": len(news_list),
        "positive_ratio": positives / len(news_list),
        "negative_ratio": negatives / len(news_list),
    }

def analyze_sentiment(news_list):
    detail = analyze_sentiment_detail(news_list)
    return detail["score"], detail["status"]
```

- [ ] **Step 4: Wire detail into `_do_analyze()`**

Change:

```python
s_score, s_status = analyze_sentiment(news)
```

to:

```python
sentiment = analyze_sentiment_detail(news)
s_score, s_status = sentiment["score"], sentiment["status"]
```

Add result fields:

```python
"news_count": sentiment["count"],
"news_positive_ratio": sentiment["positive_ratio"],
"news_negative_ratio": sentiment["negative_ratio"],
```

- [ ] **Step 5: Run tests and verify pass**

Run the same command as Step 2.

Expected: pass.

---

### Task 6: Final verification, commit, push, deploy

**Files:**
- Verify all touched files.

- [ ] **Step 1: Run focused tests**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path '.deps').Path
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_prediction_pipeline -v
```

Expected: all prediction pipeline tests pass.

- [ ] **Step 2: Run full tests**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path '.deps').Path
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 3: Check diff**

Run:

```powershell
git diff --check
git status --short --branch
```

Expected: no whitespace errors; only intended files modified.

- [ ] **Step 4: Commit**

Run:

```powershell
git add app.py tests/test_prediction_pipeline.py tests/test_line_flow.py tests/test_web_product.py
git commit -m "feat: add market context model features"
```

Expected: commit succeeds. If line/web tests did not change, omit those files.

- [ ] **Step 5: Push**

Run:

```powershell
git push origin main
```

Expected: `main -> main`.

- [ ] **Step 6: Deploy Cloud Run**

Run:

```powershell
gcloud run deploy line-stock-bot --source . --region asia-east1 --project line-stock-bot-498908 --allow-unauthenticated --quiet
```

Expected: new revision serves 100% traffic.

- [ ] **Step 7: Verify deployment**

Run:

```powershell
gcloud run services describe line-stock-bot --region asia-east1 --project line-stock-bot-498908 --format="value(status.latestReadyRevisionName,status.traffic[0].percent)"
Invoke-WebRequest -Uri 'https://line-stock-bot-1067991373149.asia-east1.run.app/' -UseBasicParsing -TimeoutSec 30 | Select-Object -ExpandProperty StatusCode
```

Expected: traffic is `100`; HTTP status is `200`.
