# Five-Day Prediction Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the displayed probability, validation metrics, and backtest consistently represent the next five trading days while adding low-cost Taiwan market volume and chip features.

**Architecture:** Keep the existing single-file Flask application and dependencies. Add small pure helpers inside `app.py` for targets, chronological splits, and metrics; enrich the existing FinMind request before feature calculation; then train LightGBM with out-of-sample walk-forward predictions and a final all-history model for the current signal.

**Tech Stack:** Python 3.12, pandas, NumPy, scikit-learn, LightGBM, Flask, unittest, FinMind API, yfinance fallback

---

## File Map

- Modify `app.py`: data loading, feature calculation, five-day target, walk-forward model, metrics, and user-facing wording.
- Create `tests/test_prediction_pipeline.py`: deterministic synthetic-data checks for target alignment, chronological gaps, transaction costs, missing chip data, and model output.
- Modify `requirements.txt` only if an existing import is missing; no new library is planned.

### Task 1: Prepare a Reproducible Test Runtime

**Files:**
- No tracked files

- [ ] **Step 1: Install the existing dependencies into a workspace-local directory**

```powershell
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pip install --target '.deps' -r requirements.txt
```

Expected: exit code 0 and `.deps` contains `pandas`, `sklearn`, `lightgbm`, `flask`, and `linebot`.

- [ ] **Step 2: Verify imports using the local dependency path**

```powershell
$env:PYTHONPATH=(Resolve-Path '.deps').Path
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -c "import pandas, sklearn, lightgbm, flask, linebot; print('deps-ok')"
```

Expected: `deps-ok`.

### Task 2: Add Five-Day Target, Chronological Splits, and Backtest Metrics

**Files:**
- Modify: `app.py:17-18,162-210`
- Create: `tests/test_prediction_pipeline.py`

- [ ] **Step 1: Write failing pure-logic tests**

Create `tests/test_prediction_pipeline.py` with dummy LINE credentials before importing `app` and tests equivalent to:

```python
import os
import unittest

import numpy as np
import pandas as pd

os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test")
os.environ.setdefault("LINE_CHANNEL_SECRET", "test")

from app import (
    PREDICTION_HORIZON,
    ROUND_TRIP_COST,
    add_prediction_target,
    build_time_splits,
    calc_all,
    run_ai_engine,
    score_oos_predictions,
)


class PredictionPipelineTests(unittest.TestCase):
    def test_last_five_rows_have_no_training_target(self):
        frame = pd.DataFrame({"Close": np.arange(1.0, 21.0)})
        result = add_prediction_target(frame)
        self.assertTrue(result["FUTURE_RET_5"].tail(PREDICTION_HORIZON).isna().all())
        self.assertTrue(result["T"].tail(PREDICTION_HORIZON).isna().all())
        self.assertEqual(int(result["T"].notna().sum()), 15)

    def test_walk_forward_splits_keep_five_row_gap(self):
        for train, test in build_time_splits(120):
            self.assertLess(train[-1], test[0])
            self.assertGreaterEqual(test[0] - train[-1] - 1, PREDICTION_HORIZON)

    def test_backtest_uses_five_day_returns_and_cost(self):
        future = pd.Series([0.02] * 10)
        probabilities = pd.Series([0.7] * 10)
        metrics = score_oos_predictions(future, probabilities)
        expected = ((1 + 0.02 - ROUND_TRIP_COST) ** 2 - 1) * 100
        self.assertAlmostEqual(metrics["strat_cum"], expected, places=8)
        self.assertEqual(metrics["trades"], 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests and verify they fail because helpers are missing**

```powershell
$env:PYTHONPATH=(Resolve-Path '.deps').Path
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_prediction_pipeline -v
```

Expected: import failure for `PREDICTION_HORIZON` or `add_prediction_target`.

- [ ] **Step 3: Implement the minimal pure helpers in `app.py`**

Add `TimeSeriesSplit` and constants, then implement these interfaces:

```python
from sklearn.model_selection import TimeSeriesSplit

PREDICTION_HORIZON = 5
ROUND_TRIP_COST = 0.00585
ENTRY_THRESHOLD = 0.60


def add_prediction_target(df):
    result = df.copy()
    future = result["Close"].shift(-PREDICTION_HORIZON) / result["Close"] - 1
    result["FUTURE_RET_5"] = future
    result["T"] = np.where(future.notna(), (future > 0).astype(float), np.nan)
    return result


def build_time_splits(n_samples):
    return list(TimeSeriesSplit(n_splits=5, gap=PREDICTION_HORIZON).split(np.arange(n_samples)))


def score_oos_predictions(future_returns, probabilities):
    frame = pd.DataFrame({"future": future_returns, "prob": probabilities}).dropna()
    target = (frame["future"] > 0).astype(int)
    sampled = frame.iloc[::PREDICTION_HORIZON]
    entries = sampled["prob"] >= ENTRY_THRESHOLD
    strategy_returns = np.where(entries, sampled["future"] - ROUND_TRIP_COST, 0.0)
    cumulative = np.cumprod(1 + strategy_returns)
    buy_hold = np.cumprod(1 + sampled["future"].to_numpy())
    active = sampled.loc[entries, "future"] - ROUND_TRIP_COST
    mdd = (cumulative / np.maximum.accumulate(cumulative) - 1).min() * 100 if len(cumulative) else 0.0
    std = np.std(strategy_returns)
    return {
        "days": len(frame),
        "accuracy": ((frame["prob"] >= 0.5).astype(int) == target).mean() * 100,
        "brier": np.mean((frame["prob"] - target) ** 2),
        "strat_cum": (cumulative[-1] - 1) * 100 if len(cumulative) else 0.0,
        "bh_cum": (buy_hold[-1] - 1) * 100 if len(buy_hold) else 0.0,
        "win_rate": (active > 0).mean() * 100 if len(active) else 0.0,
        "trades": int(entries.sum()),
        "mdd": mdd,
        "sharpe": np.mean(strategy_returns) / std * np.sqrt(252 / PREDICTION_HORIZON) if std else 0.0,
    }
```

- [ ] **Step 4: Run tests and verify the three tests pass**

Run the Task 2 Step 2 command.

Expected: `Ran 3 tests` and `OK`.

- [ ] **Step 5: Commit the pure pipeline helpers**

```powershell
git add app.py tests/test_prediction_pipeline.py
git commit -m "fix: align prediction target and backtest horizon"
```

### Task 3: Preserve OHLCV and Add Optional FinMind Chip Data

**Files:**
- Modify: `app.py:56-118,132-160`
- Test: `tests/test_prediction_pipeline.py`

- [ ] **Step 1: Add a failing missing-chip-data feature test**

Add a synthetic 100-row OHLCV frame and assert `calc_all` produces finite values for `VOL_RATIO`, `INST_NET_RATIO`, `MARGIN_CHG`, and `SHORT_CHG` even when chip columns are absent.

```python
    def test_missing_chip_data_falls_back_to_neutral_features(self):
        close = np.linspace(50, 80, 100) + np.sin(np.arange(100))
        frame = pd.DataFrame({
            "Open": close - 0.2,
            "High": close + 0.5,
            "Low": close - 0.5,
            "Close": close,
            "Volume": np.linspace(1000, 2000, 100),
        })
        result = calc_all(frame)
        columns = ["VOL_RATIO", "INST_NET_RATIO", "MARGIN_CHG", "SHORT_CHG"]
        self.assertFalse(result[columns].isna().any().any())
        self.assertTrue(np.isfinite(result[columns].to_numpy()).all())
```

- [ ] **Step 2: Run the test and verify it fails on missing feature columns**

Run the Task 2 test command.

Expected: failure referencing `VOL_RATIO` or another new column.

- [ ] **Step 3: Refactor `get_data` without adding dependencies**

Implement `_fetch_finmind_dataset(dataset, code, start_date, end_date)` using the existing login token and eight-second timeout. Build the price frame with `Volume` from `Trading_Volume`; fetch and left-join:

```python
institutional.groupby("date").agg({"buy": "sum", "sell": "sum"})
institutional_net = buy - sell
margin = margin[["date", "MarginPurchaseTodayBalance", "ShortSaleTodayBalance"]]
```

Rename the merged columns to `Date`, `InstitutionalNet`, `MarginBalance`, and `ShortBalance`. Skip chip requests for `TAIEX`. If either dataset is unavailable, create the corresponding columns with `0.0`. For Yahoo fallback, try `f"{code}.TW"` and then `f"{code}.TWO"`, retaining `Volume`.

- [ ] **Step 4: Add finite volume and chip features to `calc_all`**

Add these columns and include them in the model feature list:

```python
df["RET_5"] = c.pct_change(5)
df["RET_20"] = c.pct_change(20)
df["RANGE_PCT"] = (df["High"] - df["Low"]) / (c.abs() + 1e-9)
df["VOL_RATIO"] = df["Volume"].rolling(5).mean() / (df["Volume"].rolling(20).mean() + 1e-9)
df["VOL_CHG"] = df["Volume"].pct_change(fill_method=None).replace([np.inf, -np.inf], 0).fillna(0).clip(-5, 5)
df["INST_NET_RATIO"] = (df["InstitutionalNet"] / (df["Volume"] + 1e-9)).clip(-5, 5)
df["MARGIN_CHG"] = df["MarginBalance"].replace(0, np.nan).pct_change(fill_method=None).replace([np.inf, -np.inf], 0).fillna(0).clip(-1, 1)
df["SHORT_CHG"] = df["ShortBalance"].replace(0, np.nan).pct_change(fill_method=None).replace([np.inf, -np.inf], 0).fillna(0).clip(-1, 1)
```

- [ ] **Step 5: Run tests and verify all four tests pass**

Run the Task 2 test command.

Expected: `Ran 4 tests` and `OK`.

- [ ] **Step 6: Commit the data enrichment**

```powershell
git add app.py tests/test_prediction_pipeline.py
git commit -m "feat: add volume and Taiwan market chip features"
```

### Task 4: Replace the Single Split with Walk-Forward Predictions

**Files:**
- Modify: `app.py:162-315`
- Test: `tests/test_prediction_pipeline.py`

- [ ] **Step 1: Add a failing end-to-end model test**

Create 260 deterministic OHLCV rows with alternating cycles, call `calc_all`, then `run_ai_engine`, and assert:

```python
    def test_walk_forward_engine_returns_oos_metrics_and_current_probability(self):
        x = np.arange(260)
        close = 100 + x * 0.04 + np.sin(x / 4) * 4
        raw = pd.DataFrame({
            "Open": close - 0.2,
            "High": close + 0.8,
            "Low": close - 0.8,
            "Close": close,
            "Volume": 1000 + (x % 30) * 20,
        })
        enriched = calc_all(raw)
        metrics = run_ai_engine(enriched)
        self.assertIsNotNone(metrics)
        self.assertIn("accuracy", metrics)
        self.assertIn("brier", metrics)
        self.assertTrue(0 <= enriched["AI_P"].iloc[-1] <= 100)
        self.assertGreater(enriched["AI_P"].notna().sum(), 1)
```

- [ ] **Step 2: Run the test and verify the old engine fails the new expectations**

Run the Task 2 test command.

Expected: missing `accuracy` or `brier`, or the aligned target assertion fails.

- [ ] **Step 3: Implement walk-forward training in `run_ai_engine`**

- Use `add_prediction_target(df)` and drop only rows missing the fixed feature list, `FUTURE_RET_5`, or `T`.
- For every `build_time_splits` fold, fit LightGBM on the training indices and write probabilities only to the validation indices.
- Skip folds whose training label has fewer than two classes.
- Require at least 30 valid out-of-sample predictions.
- Call `score_oos_predictions` with aligned five-day returns and probabilities.
- Fit one final LightGBM model on all labeled rows, calculate feature importance, and predict only the latest row.
- Store out-of-sample history plus the latest prediction in `df["AI_P"]`; do not fill historical gaps with in-sample predictions.
- Remove `StandardScaler`, which does not benefit tree splits.

- [ ] **Step 4: Remove the unvalidated sentiment probability adjustment**

Delete the `+2/-2` mutation in `_do_analyze`; sentiment remains a separately displayed field.

- [ ] **Step 5: Run the complete unit test file**

Run the Task 2 test command.

Expected: all five tests pass.

- [ ] **Step 6: Commit the walk-forward model**

```powershell
git add app.py tests/test_prediction_pipeline.py
git commit -m "feat: validate stock forecasts with walk-forward splits"
```

### Task 5: Update User-Facing Metrics and Verify the Application

**Files:**
- Modify: `app.py:322-447,505-658,746-799`
- Test: `tests/test_prediction_pipeline.py`

- [ ] **Step 1: Update all probability labels**

Replace user-facing variants of `AI 勝率` and `預測勝率` with `五日上漲機率`. Add `五日方向準確率` and `Brier Score` cards to the web report. Keep the disclaimer and sentiment display unchanged.

- [ ] **Step 2: Exclude missing historical probabilities from chart data**

Build `prob_h` with `.dropna()` before JSON serialization so the chart contains only out-of-sample points and the latest current prediction.

- [ ] **Step 3: Run unit tests and Python compilation**

```powershell
$env:PYTHONPATH=(Resolve-Path '.deps').Path
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests -v
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m py_compile app.py
```

Expected: all tests pass and compilation exits 0.

- [ ] **Step 4: Run a local Flask health-check smoke test**

```powershell
$env:LINE_CHANNEL_ACCESS_TOKEN='test'
$env:LINE_CHANNEL_SECRET='test'
$env:PYTHONPATH=(Resolve-Path '.deps').Path
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -c "from app import app; r=app.test_client().get('/'); assert r.status_code == 200; print(r.get_data(as_text=True))"
```

Expected: `AI Stock Bot is awake and running!`.

- [ ] **Step 5: Inspect the final diff and commit UI changes**

```powershell
git diff --check
git diff --stat
git add app.py tests/test_prediction_pipeline.py
git commit -m "ui: clarify five-day forecast metrics"
```

### Task 6: Final Verification

**Files:**
- No new files

- [ ] **Step 1: Run fresh full verification**

Run Task 5 Steps 3 and 4 again after the final commit.

- [ ] **Step 2: Confirm repository state**

```powershell
git status --short --branch
git log -5 --oneline --decorate
```

Expected: no modified tracked files; local `main` is ahead of `origin/main` by the new documentation and implementation commits.
