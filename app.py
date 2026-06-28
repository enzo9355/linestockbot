# app.py
# v5.8 穩定版：新增首頁健康檢查端點防休眠，並梳理重複路由確保 Flask 正常啟動
# --------------------------------------------------

import os
import queue
import re
import threading
import time
import datetime
import hmac
from email.utils import parsedate_to_datetime
from html import escape
import requests
import pandas as pd
import twstock
from defusedxml import ElementTree as ET
import urllib.parse
import numpy as np
import json
import google.generativeai as genai

from sklearn.model_selection import TimeSeriesSplit
from lightgbm import LGBMClassifier
from flask import Flask, request, abort, render_template, jsonify, redirect
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, PostbackEvent, TextMessage, TextSendMessage, FlexSendMessage,
    QuickReply, QuickReplyButton, MessageAction
)
from line_state import (
    FirestoreStore, StateError, StoreError, add_alert, add_watch,
    consume_pending, evaluate_alert, remove_watch, start_pending, top_signals,
)

# ==================================================
# 1. 基本設定與系統快取
# ==================================================
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
FINMIND_USER = os.getenv("FINMIND_USER")
FINMIND_PASSWORD = os.getenv("FINMIND_PASSWORD")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")
LOCAL_HOST = os.getenv("HOST", "127.0.0.1")
BROADCAST_TOKEN = os.getenv("BROADCAST_TOKEN")
ALERT_TASK_TOKEN = os.getenv("ALERT_TASK_TOKEN")
OPENALICE_API_URL = os.getenv("OPENALICE_API_URL")
OPENALICE_API_TOKEN = os.getenv("OPENALICE_API_TOKEN")
LINE_STATE_READ_BUDGET_SECONDS = 0.25
LINE_STATE_READ_MAX_WORKERS = 4
_line_state_read_slots = threading.BoundedSemaphore(LINE_STATE_READ_MAX_WORKERS)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
line_store = FirestoreStore(GCP_PROJECT_ID) if GCP_PROJECT_ID else None

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-2.5-flash')
else:
    gemini_model = None

finmind_token = None
CATEGORY_PAGE_SIZE = 12
SECTOR_SCAN_LIMIT = 20
SECTOR_DISPLAY_LIMIT = 10
SECTOR_SNAPSHOT_DOC = "sector_signals"
PAPI_THEME_SECTORS = {
    "AI伺服器": {"鴻海", "廣達", "緯創", "緯穎", "英業達", "仁寶", "和碩", "神達", "勤誠"},
    "PC／筆電": {"華碩", "宏碁", "微星", "技嘉", "神基", "藍天"},
    "散熱機構": {"雙鴻", "奇鋐", "建準", "勤誠", "營邦", "迎廣"},
    "工業電腦": {"研華", "樺漢", "凌華", "友通", "艾訊"},
    "網通設備": {"智邦", "啟碁", "中磊", "正文", "台揚", "明泰"},
    "半導體製造": {"台積電", "聯電", "世界", "力積電"},
    "IC設計ASIC": {"聯發科", "瑞昱", "創意", "世芯-KY", "力旺", "M31"},
    "封測設備": {"日月光投控", "矽格", "京元電子", "辛耘", "弘塑", "家登"},
}
PREDICTION_HORIZON = 5
ROUND_TRIP_COST = 0.00585
ENTRY_THRESHOLD = 0.60
MARKET_FEATURES = [
    "MARKET_RET_1", "MARKET_RET_5", "MARKET_RET_20", "MARKET_VOL_20",
    "ETF50_RET_5", "STOCK_VS_MARKET_5",
]
DATA_QUALITY_FEATURES = ["DATA_PRICE_DIFF_PCT", "DATA_PRICE_WARNING"]
PRICE_DIFF_WARNING_THRESHOLD = 0.02
MODEL_FEATURES = [
    "MA_5", "MA20", "RET_1", "RET_5", "RET_20", "RSI", "Volat",
    "RANGE_PCT", "VOL_RATIO", "VOL_CHG", "INST_NET_RATIO", "MARGIN_CHG",
    "SHORT_CHG", "MACD_OSC", "K", "D",
] + MARKET_FEATURES + DATA_QUALITY_FEATURES

_SYSTEM_CACHE = {}
CACHE_EXPIRY_SECONDS = 3600
_YFINANCE_CACHE = {}
YFINANCE_CACHE_SECONDS = 3600

# ==================================================
# 2. 資料抓取與清洗模組
# ==================================================
def finmind_login():
    global finmind_token
    if finmind_token or not FINMIND_USER or not FINMIND_PASSWORD: return
    try:
        r = requests.post(
            "https://api.finmindtrade.com/api/v4/login",
            data={"user_id": FINMIND_USER, "password": FINMIND_PASSWORD},
            timeout=5
        ).json()
        if r.get("msg") == "success": finmind_token = r["token"]
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return

def fetch_finmind_dataset(dataset, code, start_date, end_date):
    finmind_login()
    params = {
        "dataset": dataset,
        "data_id": code,
        "start_date": start_date,
        "end_date": end_date,
    }
    if finmind_token:
        params["token"] = finmind_token
    try:
        response = requests.get(
            "https://api.finmindtrade.com/api/v4/data",
            params=params,
            timeout=8,
        )
        response.raise_for_status()
        return pd.DataFrame(response.json().get("data", []))
    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"FinMind {dataset} 讀取失敗: {exc}")
        return pd.DataFrame()


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
            hist = yf.download(
                ticker,
                start=start_date,
                end=end_date,
                progress=False,
                threads=False,
            )
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

def get_stock_name(code):
    if code == "TAIEX": return "台股大盤"
    if code in twstock.codes: return twstock.codes[code].name
    return code

def search_stock_code(keyword):
    keyword = keyword.upper().strip()
    if keyword in ["TAIEX", "加權指數", "台股大盤", "大盤"]: return "TAIEX", "台股大盤"
    if keyword.isdigit(): return keyword, get_stock_name(keyword)
    for code, info in twstock.codes.items():
        if keyword in info.name.upper(): return code, info.name
    return None, None

def _foreign_flow_mask(frame):
    mask = pd.Series(False, index=frame.index)
    for column in ("name", "institutional_investor", "institutional_investors", "type"):
        if column in frame:
            mask |= frame[column].astype(str).str.contains("Foreign|外資", case=False, regex=True, na=False)
    return mask


def merge_chip_data(price, institutional=None, margin=None):
    result = price.copy()
    if institutional is not None and not institutional.empty:
        flows = institutional.copy()
        flows["Date"] = pd.to_datetime(flows["date"], errors="coerce")
        flows["buy"] = pd.to_numeric(flows["buy"], errors="coerce").fillna(0)
        flows["sell"] = pd.to_numeric(flows["sell"], errors="coerce").fillna(0)
        flows["InstitutionalNet"] = flows["buy"] - flows["sell"]
        foreign_mask = _foreign_flow_mask(flows)
        foreign = flows.loc[foreign_mask] if foreign_mask.any() else flows
        foreign = foreign.groupby("Date", as_index=False)["InstitutionalNet"].sum()
        foreign = foreign.rename(columns={"InstitutionalNet": "ForeignNet"})
        flows = flows.groupby("Date", as_index=False)["InstitutionalNet"].sum()
        result = result.merge(flows, on="Date", how="left")
        result = result.merge(foreign, on="Date", how="left")
    if margin is not None and not margin.empty:
        balances = margin.copy()
        balances["Date"] = pd.to_datetime(balances["date"], errors="coerce")
        balances = balances.rename(
            columns={
                "MarginPurchaseTodayBalance": "MarginBalance",
                "ShortSaleTodayBalance": "ShortBalance",
            }
        )
        balances = balances[["Date", "MarginBalance", "ShortBalance"]]
        balances[["MarginBalance", "ShortBalance"]] = balances[
            ["MarginBalance", "ShortBalance"]
        ].apply(pd.to_numeric, errors="coerce")
        balances = balances.groupby("Date", as_index=False).last()
        result = result.merge(balances, on="Date", how="left")
    for column in ["InstitutionalNet", "ForeignNet", "MarginBalance", "ShortBalance"]:
        if column not in result:
            result[column] = 0.0
        result[column] = result[column].fillna(0.0)
    return result


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
    daily_return = close.pct_change(fill_method=None)
    frame[f"{prefix}_RET_1"] = daily_return
    frame[f"{prefix}_RET_5"] = close.pct_change(5, fill_method=None)
    frame[f"{prefix}_RET_20"] = close.pct_change(20, fill_method=None)
    frame[f"{prefix}_VOL_20"] = daily_return.rolling(20).std()
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
        result = result.merge(market_frame, on="Date", how="left")

    etf_frame = _market_feature_frame(etf50, "ETF50")
    if not etf_frame.empty and "ETF50_RET_5" in etf_frame:
        result = result.merge(etf_frame[["Date", "ETF50_RET_5"]], on="Date", how="left")

    if "Close" in result:
        stock_ret_5 = pd.to_numeric(result["Close"], errors="coerce").pct_change(5, fill_method=None)
    else:
        stock_ret_5 = pd.Series(0.0, index=result.index)
    if "MARKET_RET_5" in result:
        market_ret_5 = pd.to_numeric(result["MARKET_RET_5"], errors="coerce")
    else:
        market_ret_5 = pd.Series(0.0, index=result.index)
    result["STOCK_VS_MARKET_5"] = stock_ret_5 - market_ret_5

    for column in MARKET_FEATURES:
        if column not in result:
            result[column] = 0.0
        result[column] = (
            pd.to_numeric(result[column], errors="coerce")
            .replace([np.inf, -np.inf], 0)
            .fillna(0.0)
        )
    return result


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
    result["DATA_PRICE_WARNING"] = (
        result["DATA_PRICE_DIFF_PCT"] > PRICE_DIFF_WARNING_THRESHOLD
    ).astype(float)
    return result


def _clean_df(df):
    df[['Open', 'High', 'Low', 'Close']] = df[['Open', 'High', 'Low', 'Close']].replace(0, np.nan)
    numeric_columns = [
        "Volume", "InstitutionalNet", "ForeignNet", "MarginBalance", "ShortBalance",
    ] + MARKET_FEATURES + DATA_QUALITY_FEATURES
    for column in numeric_columns:
        if column not in df:
            df[column] = 0.0
        df[column] = (
            pd.to_numeric(df[column], errors="coerce")
            .replace([np.inf, -np.inf], 0)
            .fillna(0.0)
        )
    df = df.dropna(subset=['Date', 'Close'])
    return df.sort_values('Date').drop_duplicates(subset=['Date'], keep='last').set_index("Date")


def _annualized_percent(total_percent, days):
    if not days or days <= 0 or total_percent <= -100:
        return None
    return ((1 + total_percent / 100) ** (252 / days) - 1) * 100


def calculate_investment_projection(amount, data):
    try:
        amount = float(amount)
        price = float(data["price"])
        bt = data["bt"]
    except (KeyError, TypeError, ValueError):
        return {"ok": False}
    if amount <= 0 or price <= 0:
        return {"ok": False}
    shares = int(amount // price)
    if shares <= 0:
        return {"ok": False}
    deployed = shares * price
    strat = float(bt.get("strat_cum", 0))
    buy_hold = float(bt.get("bh_cum", 0))
    days = int(bt.get("days", 0))
    return {
        "ok": True,
        "amount": amount,
        "shares": shares,
        "deployed_amount": deployed,
        "strategy_profit": deployed * strat / 100,
        "buy_hold_profit": deployed * buy_hold / 100,
        "strategy_annualized": _annualized_percent(strat, days),
        "buy_hold_annualized": _annualized_percent(buy_hold, days),
    }


def summarize_foreign_flow(df):
    if "ForeignNet" not in df or df["ForeignNet"].abs().sum() == 0:
        return {"available": False, "net_5": 0.0, "net_20": 0.0, "status": "資料不足", "source": "外資"}
    net = pd.to_numeric(df["ForeignNet"], errors="coerce").fillna(0)
    net_5 = float(net.tail(5).sum())
    net_20 = float(net.tail(20).sum())
    if net_5 > 0 and net_20 > 0:
        status = "外資偏多"
    elif net_5 < 0 and net_20 < 0:
        status = "外資偏空"
    else:
        status = "外資中性"
    return {"available": True, "net_5": net_5, "net_20": net_20, "status": status, "source": "外資"}

def get_data(code, days=730):
    start_date = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    end_date = datetime.datetime.now().strftime("%Y-%m-%d")
    yf_price = pd.DataFrame()
    if code != "TAIEX":
        yf_price = fetch_yfinance_price_history([f"{code}.TW", f"{code}.TWO"], start_date, end_date)

    raw = fetch_finmind_dataset(
        "TaiwanStockPrice", code, start_date, end_date
    )
    price = None
    if not raw.empty:
        price = pd.DataFrame(
            {
                "Date": pd.to_datetime(raw["date"], errors="coerce"),
                "Open": pd.to_numeric(raw["open"], errors="coerce"),
                "High": pd.to_numeric(raw["max"], errors="coerce"),
                "Low": pd.to_numeric(raw["min"], errors="coerce"),
                "Close": pd.to_numeric(raw["close"], errors="coerce"),
                "Volume": pd.to_numeric(raw.get("Trading_Volume", 0), errors="coerce"),
            }
        )
    if price is None:
        price = (
            fetch_yfinance_price_history("^TWII", start_date, end_date)
            if code == "TAIEX"
            else yf_price
        )
    if price is None or price.empty:
        return pd.DataFrame()

    price = add_price_quality_features(price, yf_price)
    market = fetch_yfinance_price_history("^TWII", start_date, end_date)
    etf50 = fetch_yfinance_price_history("0050.TW", start_date, end_date)
    price = add_market_context_features(price, market, etf50)

    institutional = margin = None
    if code != "TAIEX":
        institutional = fetch_finmind_dataset(
            "TaiwanStockInstitutionalInvestorsBuySell",
            code,
            start_date,
            end_date,
        )
        margin = fetch_finmind_dataset(
            "TaiwanStockMarginPurchaseShortSale",
            code,
            start_date,
            end_date,
        )
    return _clean_df(merge_chip_data(price, institutional, margin))

# ==================================================
# 3. 核心運算模組 (LGBM)
# ==================================================
def fetch_news_rss(name):
    q = urllib.parse.quote(f"{name} 股票")
    url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    return requests.get(url, timeout=5).text


def parse_news_items(xml, now=None):
    root = ET.fromstring(xml)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    now = (
        now.replace(tzinfo=datetime.timezone.utc)
        if now.tzinfo is None
        else now.astimezone(datetime.timezone.utc)
    )
    items = []
    for node in root.findall(".//item")[:20]:
        title = (node.findtext("title") or "").strip()
        if not title:
            continue
        source = (node.findtext("source") or "").strip() or None
        published_text = (node.findtext("pubDate") or "").strip()
        published_at = age_hours = None
        if published_text:
            try:
                published = parsedate_to_datetime(published_text)
                published = (
                    published.replace(tzinfo=datetime.timezone.utc)
                    if published.tzinfo is None
                    else published.astimezone(datetime.timezone.utc)
                )
                published_at = published.isoformat()
                age_hours = max(0.0, (now - published).total_seconds() / 3600)
            except (TypeError, ValueError, OverflowError):
                pass
        items.append({
            "title": title,
            "normalized_title": title,
            "link": (node.findtext("link") or "").strip(),
            "source": source,
            "published_at": published_at,
            "age_hours": age_hours,
            "parse_flags": {
                "missing_source": source is None,
                "missing_published_at": published_at is None,
            },
            "duplicate_count": 0,
        })
    return items


def normalize_and_dedupe(items):
    kept = []
    by_title = {}
    for original in items:
        item = dict(original)
        title = " ".join(str(item.get("title", "")).split())
        source = item.get("source")
        suffix = f" - {source}" if source else ""
        if suffix and title.casefold().endswith(suffix.casefold()):
            title = title[:-len(suffix)].rstrip()
        key = re.sub(r"[\W_]+", "", title.casefold())
        if not key:
            continue
        if key in by_title:
            by_title[key]["duplicate_count"] += 1
            continue
        item["normalized_title"] = title
        item["duplicate_count"] = int(item.get("duplicate_count", 0))
        by_title[key] = item
        kept.append(item)
    return kept


def get_news(name):
    try:
        return normalize_and_dedupe(parse_news_items(fetch_news_rss(name)))[:5]
    except Exception:
        return []

def calc_all(df):
    df = df.copy()
    numeric_columns = [
        "Volume", "InstitutionalNet", "ForeignNet", "MarginBalance", "ShortBalance",
    ] + MARKET_FEATURES + DATA_QUALITY_FEATURES
    for column in numeric_columns:
        if column not in df:
            df[column] = 0.0
        df[column] = (
            pd.to_numeric(df[column], errors="coerce")
            .replace([np.inf, -np.inf], 0)
            .fillna(0.0)
        )
    c = df["Close"]
    df['MA_5'], df['MA20'], df['RET_1'] = c.rolling(5).mean(), c.rolling(20).mean(), c.pct_change(fill_method=None)
    df['RET_5'], df['RET_20'] = c.pct_change(5, fill_method=None), c.pct_change(20, fill_method=None)
    df['RANGE_PCT'] = (df['High'] - df['Low']) / (c.abs() + 1e-9)
    df['VOL_RATIO'] = df['Volume'].rolling(5).mean() / (df['Volume'].rolling(20).mean() + 1e-9)
    df['VOL_CHG'] = df['Volume'].pct_change(fill_method=None).replace([np.inf, -np.inf], 0).fillna(0).clip(-5, 5)
    df['INST_NET_RATIO'] = (df['InstitutionalNet'] / (df['Volume'] + 1e-9)).clip(-5, 5)
    df['MARGIN_CHG'] = df['MarginBalance'].replace(0, np.nan).pct_change(fill_method=None).replace([np.inf, -np.inf], 0).fillna(0).clip(-1, 1)
    df['SHORT_CHG'] = df['ShortBalance'].replace(0, np.nan).pct_change(fill_method=None).replace([np.inf, -np.inf], 0).fillna(0).clip(-1, 1)
    d = c.diff()
    g, l = d.clip(lower=0).rolling(14).mean(), -d.clip(upper=0).rolling(14).mean()
    df["RSI"] = 100 - (100 / (1 + (g / (l + 1e-9))))
    df['Volat'] = df['RET_1'].rolling(20).std()
    
    # MACD
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df['MACD_DIF'] = ema12 - ema26
    df['MACD'] = df['MACD_DIF'].ewm(span=9, adjust=False).mean()
    df['MACD_OSC'] = df['MACD_DIF'] - df['MACD']
    
    # KD
    high9 = df['High'].rolling(9).max()
    low9 = df['Low'].rolling(9).min()
    rsv = (c - low9) / (high9 - low9 + 1e-9) * 100
    df['K'] = rsv.ewm(com=2, adjust=False).mean()
    df['D'] = df['K'].ewm(com=2, adjust=False).mean()
    
    # Bollinger Bands
    std20 = c.rolling(20).std()
    df['BB_UP'] = df['MA20'] + 2 * std20
    df['BB_DN'] = df['MA20'] - 2 * std20
    
    return df.dropna()

def add_prediction_target(df):
    result = df.copy()
    future = result["Close"].shift(-PREDICTION_HORIZON) / result["Close"] - 1
    result["FUTURE_RET_5"] = future
    result["T"] = np.where(future.notna(), (future > 0).astype(float), np.nan)
    return result

def build_time_splits(n_samples):
    splitter = TimeSeriesSplit(n_splits=5, gap=PREDICTION_HORIZON)
    return list(splitter.split(np.arange(n_samples)))

def score_oos_predictions(future_returns, probabilities):
    frame = pd.DataFrame({"future": future_returns, "prob": probabilities}).dropna()
    target = (frame["future"] > 0).astype(int)
    sampled = frame.iloc[::PREDICTION_HORIZON]
    entries = sampled["prob"] >= ENTRY_THRESHOLD
    strategy_returns = np.where(
        entries,
        sampled["future"] - ROUND_TRIP_COST,
        0.0,
    )
    cumulative = np.cumprod(1 + strategy_returns)
    buy_hold = np.cumprod(1 + sampled["future"].to_numpy())
    active = sampled.loc[entries, "future"] - ROUND_TRIP_COST
    mdd = (
        (cumulative / np.maximum.accumulate(cumulative) - 1).min() * 100
        if len(cumulative)
        else 0.0
    )
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
        "sharpe": (
            np.mean(strategy_returns) / std * np.sqrt(252 / PREDICTION_HORIZON)
            if std
            else 0.0
        ),
    }

def run_ai_engine(df):
    try:
        training = add_prediction_target(df).dropna(
            subset=MODEL_FEATURES + ["FUTURE_RET_5", "T"]
        )
        if len(training) < 100 or training["T"].nunique() < 2:
            return None

        oos_prob = pd.Series(np.nan, index=training.index, dtype=float)
        for train_index, test_index in build_time_splits(len(training)):
            fold = training.iloc[train_index]
            if fold["T"].nunique() < 2:
                continue
            model = LGBMClassifier(
                n_estimators=80,
                learning_rate=0.05,
                max_depth=4,
                random_state=42,
                verbose=-1,
            )
            model.fit(fold[MODEL_FEATURES], fold["T"].astype(int))
            oos_prob.iloc[test_index] = model.predict_proba(
                training.iloc[test_index][MODEL_FEATURES]
            )[:, 1]

        valid = oos_prob.notna()
        if valid.sum() < 30:
            return None
        metrics = score_oos_predictions(
            training.loc[valid, "FUTURE_RET_5"],
            oos_prob.loc[valid],
        )

        final_model = LGBMClassifier(
            n_estimators=80,
            learning_rate=0.05,
            max_depth=4,
            random_state=42,
            verbose=-1,
        )
        final_model.fit(training[MODEL_FEATURES], training["T"].astype(int))
        latest_probability = final_model.predict_proba(
            df.iloc[[-1]][MODEL_FEATURES]
        )[0, 1]

        df["AI_P"] = np.nan
        df.loc[oos_prob.loc[valid].index, "AI_P"] = oos_prob.loc[valid] * 100
        df.loc[df.index[-1], "AI_P"] = latest_probability * 100

        feature_names = {
            "MA_5": "5日均線動能", "MA20": "月線趨勢支撐",
            "RET_1": "單日反轉動能", "RET_5": "5日價格動能",
            "RET_20": "月報酬動能", "RSI": "RSI 強弱度",
            "Volat": "波動收斂度", "RANGE_PCT": "日內振幅",
            "VOL_RATIO": "成交量趨勢", "VOL_CHG": "成交量變化",
            "INST_NET_RATIO": "法人買賣超", "MARGIN_CHG": "融資變化",
            "SHORT_CHG": "融券變化", "MACD_OSC": "MACD 柱狀體動能",
            "K": "KD K值", "D": "KD D值",
            "MARKET_RET_1": "大盤單日動能",
            "MARKET_RET_5": "大盤5日動能",
            "MARKET_RET_20": "大盤月動能",
            "MARKET_VOL_20": "大盤波動度",
            "ETF50_RET_5": "0050五日動能",
            "STOCK_VS_MARKET_5": "個股相對大盤強度",
            "DATA_PRICE_DIFF_PCT": "資料源價差",
            "DATA_PRICE_WARNING": "資料品質警示",
        }
        importances = final_model.feature_importances_
        total_importance = max(float(importances.sum()), 1.0)
        metrics["top_features"] = [
            f"{feature_names.get(feature, feature)} (貢獻度: {importance / total_importance * 100:.1f}%)"
            for feature, importance in sorted(
                zip(MODEL_FEATURES, importances),
                key=lambda item: item[1],
                reverse=True,
            )[:3]
        ]
        if metrics["trades"] == 0:
            metrics["conclusion"] = "⏸️ 訊號空窗：模型未發現高勝率進場點，選擇空手觀望。"
        elif metrics["strat_cum"] > metrics["bh_cum"]:
            metrics["conclusion"] = (
                "✅ 策略優勢：高報酬且風險控制優異。"
                if metrics["sharpe"] > 1
                else "✅ 擊敗大盤：能創造超額報酬。"
            )
        else:
            metrics["conclusion"] = (
                "🛡️ 下檔保護：大跌時具備避險作用。"
                if metrics["mdd"] > -15
                else "⚠️ 模型失真：容易追高殺低。"
            )
        return metrics
    except Exception as e:
        print(f"回測引擎錯誤: {e}")
        return None

def get_ai_insight_for_broadcast(name, data, bt, news):
    if not gemini_model: return "未設定 API Key，無法生成觀點。"
    n_txt = "\n".join([n['title'] for n in news])
    prompt = f"""請以資深分析師語氣，針對{name}撰寫100字內洞見。不要廢話，直接給建議。
最新價:{data['price']}
五日上漲機率:{data['prob']}%
夏普值:{bt['sharpe']:.2f}
新聞:\n{n_txt}"""
    try:
        safety = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]
        response = gemini_model.generate_content(prompt, safety_settings=safety)
        return response.text.strip() if response.text else "AI 觀點生成為空。"
    except Exception as e:
        return "暫時無法生成 AI 觀點，請參考量化數據。"

# ==================================================
# 4. 分析總控
# ==================================================
NEWS_NEGATIONS = ("不", "未", "無", "難")
NEWS_MAJOR_EVENTS = ("財報", "營收", "財測", "法說", "政策", "違約", "訴訟", "併購")
NEWS_OPINION_TERMS = ("傳聞", "預估", "預測", "看好", "看壞", "可能", "有望")
NEWS_SENTIMENT_RULES = (
    ("營收創新高", 1, 0.5), ("上修財測", 1, 0.5),
    ("獲利創高", 1, 0.5), ("下修財測", -1, 0.5),
    ("重大虧損", -1, 0.5), ("遭降評", -1, 0.5),
    ("看好", 1, 0.2), ("成長", 1, 0.2), ("突破", 1, 0.2),
    ("新高", 1, 0.2), ("獲利", 1, 0.2), ("買超", 1, 0.2),
    ("看壞", -1, 0.2), ("下修", -1, 0.2), ("衰退", -1, 0.2),
    ("虧損", -1, 0.2), ("違約", -1, 0.2), ("降評", -1, 0.2),
    ("賣超", -1, 0.2),
)


def score_news_item(news):
    item = dict(news)
    title = str(item.get("normalized_title") or item.get("title") or "")
    matched_phrases = []
    matched_positive = []
    matched_negative = []
    matched_negations = []
    raw_score = 0.0
    for phrase, sign, value in NEWS_SENTIMENT_RULES:
        if phrase not in title:
            continue
        negated = next(
            (f"{negation}{phrase}" for negation in NEWS_NEGATIONS
             if f"{negation}{phrase}" in title),
            None,
        )
        raw_score += -sign * value if negated else sign * value
        target = (
            matched_phrases if value >= 0.5
            else matched_positive if sign > 0
            else matched_negative
        )
        target.append(phrase)
        if negated:
            matched_negations.append(negated)

    raw_score = max(-1.0, min(1.0, raw_score))
    event_type = (
        "major" if any(term in title for term in NEWS_MAJOR_EVENTS)
        else "opinion" if any(term in title for term in NEWS_OPINION_TERMS)
        else "normal"
    )
    age_hours = item.get("age_hours")
    time_weight = (
        1.0 if age_hours is not None and age_hours <= 24
        else 0.75 if age_hours is not None and age_hours <= 72
        else 0.5 if age_hours is not None and age_hours <= 168
        else 0.25
    )
    source_weight = 1.0 if item.get("source") else 0.75
    event_weight = {"major": 1.3, "normal": 1.0, "opinion": 0.7}[event_type]
    item.update({
        "raw_score": raw_score,
        "direction": (
            "positive" if raw_score > 0.1
            else "negative" if raw_score < -0.1
            else "neutral"
        ),
        "matched_phrases": matched_phrases,
        "matched_positive_terms": matched_positive,
        "matched_negative_terms": matched_negative,
        "matched_negations": matched_negations,
        "event_type": event_type,
        "time_weight": time_weight,
        "source_weight": source_weight,
        "event_weight": event_weight,
        "final_weight": time_weight * source_weight * event_weight,
    })
    return item


def aggregate_news_sentiment(items):
    if not items:
        return {
            "score": 50.0,
            "status": "中性",
            "count": 0,
            "positive_ratio": 0.0,
            "negative_ratio": 0.0,
            "neutral_ratio": 0.0,
            "confidence_score": 0.0,
            "confidence": "低",
            "items": [],
        }

    total_weight = sum(item["final_weight"] for item in items) or 1.0
    weighted_score = sum(
        item["raw_score"] * item["final_weight"] for item in items
    ) / total_weight
    score = max(0.0, min(100.0, 50.0 + 50.0 * weighted_score))
    status = (
        "極度偏多" if score >= 75
        else "偏多" if score >= 60
        else "中性" if score >= 40
        else "偏空" if score >= 25
        else "極度偏空"
    )
    count = len(items)
    positive_ratio = sum(item["direction"] == "positive" for item in items) / count
    negative_ratio = sum(item["direction"] == "negative" for item in items) / count
    fresh_ratio = sum(
        item.get("age_hours") is not None and item["age_hours"] <= 24
        for item in items
    ) / count
    source_ratio = sum(bool(item.get("source")) for item in items) / count
    confidence_score = 100 * (
        0.5 * min(count / 5, 1) + 0.3 * fresh_ratio + 0.2 * source_ratio
    )
    return {
        "score": score,
        "status": status,
        "count": count,
        "positive_ratio": positive_ratio,
        "negative_ratio": negative_ratio,
        "neutral_ratio": max(0.0, 1 - positive_ratio - negative_ratio),
        "confidence_score": confidence_score,
        "confidence": (
            "高" if confidence_score >= 75
            else "中" if confidence_score >= 45
            else "低"
        ),
        "items": items,
    }


def analyze_sentiment_detail(news_list):
    return aggregate_news_sentiment([score_news_item(item) for item in news_list])


def analyze_sentiment(news_list):
    detail = analyze_sentiment_detail(news_list)
    return detail["score"], detail["status"]

def _do_analyze(code):
    df = get_data(code)
    if df.empty or len(df) < 200: return None
    df = calc_all(df)
    bt = run_ai_engine(df)
    if not bt: return None
    
    last = df.iloc[-1]
    name = get_stock_name(code)
    news = get_news(name)

    sentiment = analyze_sentiment_detail(news)
    news = sentiment["items"]
    s_score, s_status = sentiment["score"], sentiment["status"]
    prob = last['AI_P']
    prob = int(max(0, min(100, prob)))

    trend = "多頭" if last['Close'] > last['MA20'] else "空頭"
    foreign_flow = summarize_foreign_flow(df)

    tv_df = df.copy().reset_index()
    tv_df['Date'] = tv_df['Date'].dt.strftime('%Y-%m-%d')
    tv_df['Open'] = tv_df['Open'].fillna(tv_df['Close'])
    tv_df['High'] = tv_df['High'].fillna(tv_df['Close'])
    tv_df['Low'] = tv_df['Low'].fillna(tv_df['Close'])
    tv_df['High_corr'] = tv_df[['Open', 'High', 'Low', 'Close']].max(axis=1)
    tv_df['Low_corr'] = tv_df[['Open', 'High', 'Low', 'Close']].min(axis=1)
    
    last_vol = df['Volat'].iloc[-1] if pd.notna(df['Volat'].iloc[-1]) else 0.02
    drift = ((prob - 50) / 50.0) * (last_vol * last['Close'])
    pred = [{'time': tv_df['Date'].iloc[-1], 'value': last['Close']}]
    curr_d = df.index[-1]
    curr_p = last['Close']
    for _ in range(5):
        curr_d += datetime.timedelta(days=1)
        while curr_d.weekday() >= 5: curr_d += datetime.timedelta(days=1)
        curr_p += drift
        pred.append({'time': curr_d.strftime('%Y-%m-%d'), 'value': round(curr_p, 2)})

    result = {
        "code": code, "name": name, "price": last['Close'], "prob": prob,
        "as_of": df.index[-1].date().isoformat(),
        "bt": bt, "news": news, "trend": trend,
        "rsi": last['RSI'], "ma20": last['MA20'],
        "macd_osc": last['MACD_OSC'], "k": last['K'], "d": last['D'],
        "foreign_flow": foreign_flow,
        "s_score": s_score, "s_status": s_status,
        "news_count": sentiment["count"],
        "news_positive_ratio": sentiment["positive_ratio"],
        "news_negative_ratio": sentiment["negative_ratio"],
        "news_neutral_ratio": sentiment["neutral_ratio"],
        "news_confidence_score": sentiment["confidence_score"],
        "news_confidence": sentiment["confidence"],
        "candles": json.dumps(tv_df[['Date','Open','High_corr','Low_corr','Close']].rename(columns={'Date':'time','Open':'open','High_corr':'high','Low_corr':'low','Close':'close'}).to_dict('records')),
        "ma20_line": json.dumps(tv_df[['Date','MA20']].dropna().rename(columns={'Date':'time','MA20':'value'}).to_dict('records')),
        "prob_h": json.dumps(tv_df[['Date','AI_P']].dropna().rename(columns={'Date':'time','AI_P':'value'}).to_dict('records')),
        "pred": json.dumps(pred)
    }
    result["projection"] = calculate_investment_projection(100000, result)
    return result

def analyze(code):
    now = time.time()
    if code in _SYSTEM_CACHE:
        cached_data, timestamp = _SYSTEM_CACHE[code]
        if now - timestamp < CACHE_EXPIRY_SECONDS:
            return cached_data
    data = _do_analyze(code)
    if data: _SYSTEM_CACHE[code] = (data, now)
    return data

def cached_opportunities(limit=5):
    now = time.time()
    items = []
    for code, (data, timestamp) in _SYSTEM_CACHE.items():
        if code == "TAIEX" or now - timestamp >= CACHE_EXPIRY_SECONDS:
            continue
        if all(key in data for key in ("name", "prob")):
            items.append({"code": code, "name": data["name"], "prob": data["prob"]})
    return sorted(items, key=lambda item: item["prob"], reverse=True)[:limit]

def dashboard_sector_cards(limit=6):
    try:
        snapshot = load_sector_signal_snapshot(line_store)
    except Exception:
        snapshot = {}
    cards = []
    for name, items in (snapshot or {}).get("sectors", {}).items():
        if not items:
            continue
        leader = items[0]
        cards.append({
            "name": name,
            "count": len(items),
            "score": round(_safe_float(leader.get("score")), 1),
            "leader": {
                "code": str(leader.get("code") or ""),
                "name": str(leader.get("name") or ""),
                "prob": int(_safe_float(leader.get("prob"))),
                "trend": str(leader.get("trend") or "中性"),
                "foreign_net_5": int(_safe_float(leader.get("foreign_net_5"))),
                "as_of": str(leader.get("as_of") or ""),
            },
        })
    if cards:
        return sorted(cards, key=lambda item: item["score"], reverse=True)[:limit]
    fallback = []
    for item in cached_opportunities(limit):
        fallback.append({
            "name": "熱門觀察",
            "count": 1,
            "score": float(item["prob"]),
            "leader": {
                "code": item["code"],
                "name": item["name"],
                "prob": int(item["prob"]),
                "trend": "等待更新",
                "foreign_net_5": 0,
                "as_of": "",
            },
        })
    return fallback

def dashboard_top_picks(cards, limit=3):
    picks = []
    for card in cards[:limit]:
        leader = card["leader"]
        picks.append({
            "code": leader["code"],
            "name": leader["name"],
            "headline": f"{card['name']}優先觀察",
            "summary": f"AI 勝率 {leader['prob']}%・{leader['trend']}・外資5日 {leader['foreign_net_5']:,}",
        })
    return picks

def market_forecast(): return analyze("TAIEX")

# ==================================================
# 5. UI 渲染
# ==================================================
def render_web(d):
    bt = d['bt']
    news_blocks = []
    direction_labels = {"positive": "正向", "negative": "負向", "neutral": "中性"}
    for n in d['news']:
        title = n.get("normalized_title") or n.get("title", "")
        source = n.get("source") or "來源未知"
        published = str(n.get("published_at") or "")[:10] or "時間未知"
        direction = direction_labels.get(n.get("direction"), "中性")
        news_blocks.append(
            f'<a href="{escape(str(n.get("link", "")), quote=True)}" target="_blank" rel="noopener noreferrer" class="news-link">'
            f'🔹 {escape(str(title))}<small style="display:block;color:#94a3b8;margin-top:4px;">'
            f'{escape(str(source))} · {escape(published)} · {direction}</small></a>'
        )
    news_html = "".join(news_blocks) if news_blocks else "暫無相關新聞"
    sentiment_summary = (
        f'{d.get("news_count", len(d["news"]))} 則｜'
        f'正面 {round(d.get("news_positive_ratio", 0) * 100)}%｜'
        f'負面 {round(d.get("news_negative_ratio", 0) * 100)}%｜'
        f'可信度{d.get("news_confidence", "低")}'
    )
    
    html = f"""
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{d['name']} 分析報告</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@300;400;700&display=swap" rel="stylesheet">
<script src="https://unpkg.com/lightweight-charts@4.2.2/dist/lightweight-charts.standalone.production.js"></script>
<style>
    body {{ margin:0; background: linear-gradient(135deg, #0f2027, #203a43, #2c5364); background-attachment: fixed; color: #f1f1f1; font-family: 'Noto Sans TC', sans-serif; }}
    .wrap {{ max-width:920px; margin:auto; padding:30px 20px 60px; }}
    h1 {{ font-size:42px; margin-bottom:24px; font-weight: 700; text-shadow: 0 2px 10px rgba(0,0,0,0.5); }}
    .card {{ background: rgba(255, 255, 255, 0.05); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px); border: 1px solid rgba(255, 255, 255, 0.15); box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3); border-radius: 20px; padding: 26px; margin-bottom: 24px; transition: transform 0.3s ease; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:20px; }}
    .small {{ font-size:17px; line-height:1.8; }}
    .highlight {{ color: #00f2fe; font-weight: bold; font-size: 1.1em; }}
    h2 {{ font-size: 22px; margin-top: 0; margin-bottom: 15px; border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 10px; }}
    .news-link {{ display: block; color: #e0e0e0; text-decoration: none; margin-bottom: 14px; line-height: 1.5; }}
    #tvchart {{ width: 100%; height: 450px; border-radius: 12px; overflow: hidden; margin-top: 10px; }}
</style>
</head>
<body>
<div class="wrap">
<h1>{d['name']} ({d['code']})</h1>

<div class="card small">
    💰 最新收盤：<span class="highlight">{d['price']:.2f}</span><br>
    📈 當前趨勢：{d['trend']}<br>
    🎯 五日上漲機率：<span class="highlight">{d['prob']}%</span>
</div>

<div class="card">
    <h2>📈 互動式技術線圖與預測軌跡</h2>
    <div id="tvchart"></div>
</div>

<div class="grid">
    <div class="card small" style="border-left: 4px solid #ff9800;">
        <h2 style="color: #ff9800; border-bottom: none; margin-bottom: 5px;">🤖 AI 決策核心邏輯</h2>
        <div style="font-size: 15px; color: #bbb; margin-bottom: 15px;">特徵權重解析 (Feature Importance)</div>
        <div style="background: rgba(0,0,0,0.3); padding: 15px; border-radius: 12px; margin-bottom: 10px;">🥇 <span style="color:#fff;">{bt['top_features'][0]}</span></div>
        <div style="background: rgba(0,0,0,0.3); padding: 15px; border-radius: 12px; margin-bottom: 10px;">🥈 <span style="color:#fff;">{bt['top_features'][1]}</span></div>
        <div style="background: rgba(0,0,0,0.3); padding: 15px; border-radius: 12px;">🥉 <span style="color:#fff;">{bt['top_features'][2]}</span></div>
    </div>
    <div class="card small">
        <h2>📑 指標摘要</h2>
        📈 趨勢判讀：{d['trend']}<br>
        🌊 均線狀態：{'站上 MA20 (支撐強)' if d['price'] > d['ma20'] else '跌破 MA20 (壓力大)'}<br>
        🌡 RSI 強弱：{'動能偏強' if d['rsi'] >= 55 else '中性' if d['rsi'] >= 45 else '動能偏弱'}<br>
        📊 MACD 柱狀：{'紅柱 (多頭動能)' if d['macd_osc'] > 0 else '綠柱 (空頭動能)'}<br>
        📉 KD 指標：{'黃金交叉' if d['k'] > d['d'] else '死亡交叉'}<br>
        🎯 五日上漲機率：<span class="highlight">{d['prob']}%</span>
    </div>
</div>

<div class="card small">
    <h2>📊 歷史回測報告 (近 {bt['days']} 交易日)</h2>
    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; margin-bottom: 20px;">
        <div style="background: rgba(0,0,0,0.25); padding: 15px; border-radius: 12px; text-align: center;"><div style="font-size: 13px; color: #aaa; margin-bottom: 5px;">AI 策略報酬</div><div class="highlight" style="font-size: 1.3em;">{bt['strat_cum']:.2f}%</div></div>
        <div style="background: rgba(0,0,0,0.25); padding: 15px; border-radius: 12px; text-align: center;"><div style="font-size: 13px; color: #aaa; margin-bottom: 5px;">買進持有報酬</div><div style="font-size: 1.3em; color: #ddd;">{bt['bh_cum']:.2f}%</div></div>
        <div style="background: rgba(0,0,0,0.25); padding: 15px; border-radius: 12px; text-align: center;"><div style="font-size: 13px; color: #aaa; margin-bottom: 5px;">五日方向準確率</div><div style="font-size: 1.3em; color: #ddd;">{bt['accuracy']:.1f}%</div></div>
        <div style="background: rgba(0,0,0,0.25); padding: 15px; border-radius: 12px; text-align: center;"><div style="font-size: 13px; color: #aaa; margin-bottom: 5px;">Brier Score</div><div style="font-size: 1.3em; color: #ddd;">{bt['brier']:.3f}</div></div>
        <div style="background: rgba(0,0,0,0.25); padding: 15px; border-radius: 12px; text-align: center;"><div style="font-size: 13px; color: #aaa; margin-bottom: 5px;">進場勝率</div><div style="font-size: 1.3em; color: #ddd;">{bt['win_rate']:.1f}%</div></div>
        <div style="background: rgba(0,0,0,0.25); padding: 15px; border-radius: 12px; text-align: center;"><div style="font-size: 13px; color: #aaa; margin-bottom: 5px;">交易次數</div><div style="font-size: 1.3em; color: #ddd;">{bt['trades']} 次</div></div>
        <div style="background: rgba(0,0,0,0.25); padding: 15px; border-radius: 12px; text-align: center;"><div style="font-size: 13px; color: #aaa; margin-bottom: 5px;">最大回檔</div><div style="font-size: 1.3em; color: #ff6b6b;">{bt['mdd']:.2f}%</div></div>
        <div style="background: rgba(0,0,0,0.25); padding: 15px; border-radius: 12px; text-align: center;"><div style="font-size: 13px; color: #aaa; margin-bottom: 5px;">夏普值</div><div style="font-size: 1.3em; color: #ddd;">{bt['sharpe']:.2f}</div></div>
    </div>
    <div style="background: rgba(0,242,254,0.05); border-left: 4px solid #00f2fe; padding: 18px; border-radius: 0 12px 12px 0;">
        <div style="font-weight: bold; margin-bottom: 10px; color: #00f2fe; font-size: 18px;">💡 資產管理評估</div>
        <div style="color: #e0e0e0; line-height: 1.6;">{bt['conclusion']}</div>
    </div>
</div>

<div class="card small">
    <h2>📰 相關即時新聞與情緒分析</h2>
    <div style="margin-bottom: 15px; background: rgba(255,255,255,0.05); padding: 15px; border-radius: 12px; border-left: 4px solid {'#ef5350' if d['s_score']<40 else '#26a69a'};">
        <span style="color: #aaa; font-size: 14px;">市場情緒分數</span><br>
        <span style="font-size: 24px; font-weight: bold; color: {'#ef5350' if d['s_score']<40 else '#26a69a'};">{d['s_score']:.1f} ({d['s_status']})</span><br>
        <span style="color:#94a3b8;font-size:13px;">{sentiment_summary}</span>
    </div>
    {news_html}
</div>

<div class="card small" style="background: rgba(255, 255, 255, 0.08); border-top: 4px solid #6366f1;">
    <h2 style="color: #818cf8;">📖 新手投資小辭典 (給剛接觸股市的你)</h2>
    <div style="margin-bottom: 12px;"><strong>🔹 MA20 (月均線)：</strong>就像是過去一個月的「平均成本」。股價站在上面代表多數人賺錢（趨勢偏多），跌破代表多數人賠錢。</div>
    <div style="margin-bottom: 12px;"><strong>🔹 RSI (相對強弱)：</strong>用來判斷「是不是漲太多或跌太深」。超過 70 小心過熱，低於 30 代表可能跌過頭了。</div>
    <div style="margin-bottom: 12px;"><strong>🔹 MACD (動能指標)：</strong>紅柱代表「上漲力道變強」，綠柱代表「下跌力道變強」，就像是踩油門和煞車。</div>
    <div style="margin-bottom: 12px;"><strong>🔹 KD (隨機指標)：</strong>用來抓「轉折點」。黃金交叉（K往上穿過D）是起漲訊號，死亡交叉是下跌訊號。</div>
    <div style="margin-bottom: 12px;"><strong>🔹 夏普值 (Sharpe Ratio)：</strong>這就是「CP值」。數值越高，代表承擔一樣的風險下，能賺到的錢越多！</div>
    <div><strong>🔹 最大回檔 (MDD)：</strong>也就是「歷史最大跌幅」。最倒楣的情況下，你的資產會縮水多少百分比。</div>
</div>

</div>

<script>
    try {{
        const chartContainer = document.getElementById('tvchart');
        const chartOptions = {{
            width: chartContainer.clientWidth, height: 450,
            layout: {{ backgroundColor: 'transparent', textColor: '#d1d4dc' }},
            grid: {{ vertLines: {{ color: 'rgba(42, 46, 57, 0.15)' }}, horzLines: {{ color: 'rgba(42, 46, 57, 0.15)' }} }},
            timeScale: {{ timeVisible: true }}
        }};
        const chart = LightweightCharts.createChart(chartContainer, chartOptions);

        const candleS = chart.addCandlestickSeries({{ upColor: '#ef5350', downColor: '#26a69a', borderDownColor: '#26a69a', borderUpColor: '#ef5350', wickDownColor: '#26a69a', wickUpColor: '#ef5350' }});
        const cData = {d['candles']};
        candleS.setData(cData);

        chart.addLineSeries({{ color: '#00f2fe', lineWidth: 1, title: 'MA20' }}).setData({d['ma20_line']});
        chart.addLineSeries({{ color: '#ff9800', lineWidth: 2, lineStyle: 2, title: '5日預測' }}).setData({d['pred']});

        const probS = chart.addHistogramSeries({{ priceFormat: {{ type: 'volume' }}, priceScaleId: '' }});
        chart.priceScale('').applyOptions({{ scaleMargins: {{ top: 0.8, bottom: 0 }} }});
        probS.setData({d['prob_h']}.map(x=>({{ time: x.time, value: x.value, color: x.value >= 50 ? 'rgba(38,166,154,0.4)' : 'rgba(239,83,80,0.4)' }})));
        
        if (cData.length > 120) chart.timeScale().setVisibleLogicalRange({{ from: cData.length - 120, to: cData.length + 5 }});
        
        window.addEventListener('resize', () => {{ chart.resize(chartContainer.clientWidth, 450); }});
    }} catch (err) {{
        document.getElementById('tvchart').innerHTML = "<div style='color:#ff6b6b; padding:20px;'>圖表載入失敗：" + err.message + "</div>";
    }}
</script>
</body>
</html>
"""
    return html

# ==================================================
# 6. 動態產業分類與選單生成
# ==================================================
def _safe_float(value, default=0.0):
    try:
        value = float(value)
        return value if np.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _clamp(value, low, high):
    return max(low, min(high, value))


def _is_crypto_query(text):
    normalized = text.upper()
    return any(
        keyword in normalized
        for keyword in ("BTC", "ETH", "USDT", "USDC", "CRYPTO", "虛擬貨幣", "虛擬幣", "加密貨幣", "比特幣", "以太幣")
    )


def call_openalice(prompt):
    response = requests.post(
        OPENALICE_API_URL,
        headers={"Authorization": f"Bearer {OPENALICE_API_TOKEN}"},
        json={"prompt": _build_papi_prompt(prompt)},
        timeout=4,
    )
    response.raise_for_status()
    payload = response.json()
    summary = str(
        payload.get("summary") or payload.get("text") or payload.get("message") or ""
    ).strip()
    detail_url = str(payload.get("detail_url") or payload.get("url") or "").strip()
    if not summary:
        summary = "Papi 沒有回傳可用摘要。"
    return summary + (f"\n\n詳細分析：{detail_url}" if detail_url else "")


def _extract_stock_from_papi_prompt(prompt):
    """Try to extract a stock code from a Papi prompt like '分析 2330' or '分析 台積電'."""
    m = re.search(r"分析\s+(.+)", prompt)
    if m:
        keyword = m.group(1).strip()
        code, name = search_stock_code(keyword)
        if code:
            return code, name
    # Also try if the entire prompt is just a stock code or name
    code, name = search_stock_code(prompt)
    if code:
        return code, name
    return None, None


def _match_sector_from_prompt(prompt):
    """Try to match a Papi prompt to an industry_map category.

    Returns (category_name, stock_codes_list) or (None, None).
    """
    keywords = prompt.upper()
    best_cat = None
    best_len = 0
    for cat in industry_map:
        cat_upper = cat.upper()
        if cat_upper in keywords and len(cat_upper) > best_len:
            best_cat = cat
            best_len = len(cat_upper)
    if best_cat:
        return best_cat, industry_map[best_cat]
    return None, None


def _build_single_stock_context(data):
    """Build a data context string for a single analyzed stock."""
    bt = data.get("bt", {})
    foreign = data.get("foreign_flow", {})
    foreign_str = ""
    if foreign.get("available"):
        foreign_str = f"外資買賣超：{foreign.get('status', '未知')}（近5日淨額 {foreign.get('net_5', 0):.0f}）"
    news_titles = "\n".join(
        [f"  - {n['title']}" for n in data.get("news", [])[:3]]
    )
    return (
        f"▸ {data.get('name', '?')} ({data.get('code', '?')})："
        f"收盤 {data['price']:.2f}，"
        f"AI 勝率 {data['prob']}%，"
        f"趨勢 {data['trend']}，"
        f"RSI {data['rsi']:.1f}，"
        f"{'紅柱' if data['macd_osc'] > 0 else '綠柱'}，"
        f"KD {'黃金交叉' if data['k'] > data['d'] else '死亡交叉'}，"
        f"情緒 {data['s_status']}（{data['s_score']:.0f}），"
        f"{foreign_str}，"
        f"回測策略報酬 {bt.get('strat_cum', 0):.1f}%，"
        f"勝率 {bt.get('win_rate', 0):.0f}%，"
        f"夏普 {bt.get('sharpe', 0):.2f}"
    )


def _gather_sector_data(codes, max_fresh=2, max_total=5):
    """Gather analysis data for a sector. Prioritize cache, analyze at most max_fresh new stocks.

    Returns a list of (code, data) tuples.
    """
    results = []
    fresh_count = 0
    now = time.time()

    # First pass: collect cached stocks
    for code in codes:
        if len(results) >= max_total:
            break
        if code in _SYSTEM_CACHE:
            cached_data, ts = _SYSTEM_CACHE[code]
            if now - ts < CACHE_EXPIRY_SECONDS and cached_data:
                results.append((code, cached_data))

    # Second pass: analyze a few uncached stocks if we need more
    if len(results) < max_total:
        for code in codes:
            if len(results) >= max_total or fresh_count >= max_fresh:
                break
            if any(r[0] == code for r in results):
                continue
            try:
                data = analyze(code)
                if data:
                    results.append((code, data))
                    fresh_count += 1
            except Exception:
                continue

    return results


def _build_papi_sector_examples(limit=3):
    if not line_store:
        return ""
    try:
        snapshot = load_sector_signal_snapshot(line_store)
    except Exception:
        return ""
    items = []
    for category, signals in (snapshot or {}).get("sectors", {}).items():
        for item in signals or []:
            items.append((category, item))
    items.sort(key=lambda pair: _safe_float(pair[1].get("score")), reverse=True)
    lines = []
    for category, item in items[:limit]:
        lines.append(
            f"- {item.get('name')} ({item.get('code')})：{category}，"
            f"AI 勝率 {int(_safe_float(item.get('prob')))}%，"
            f"{item.get('trend', '中性')}，"
            f"外資5日 {int(_safe_float(item.get('foreign_net_5'))):,}"
        )
    if not lines:
        return ""
    return "\n每日產業預測可舉例標的（只可從這裡挑，不要自己編）：\n" + "\n".join(lines)


def _build_papi_prompt(prompt):
    data_context = ""

    # 1. Try individual stock first
    code, name = _extract_stock_from_papi_prompt(prompt)
    if code:
        try:
            data = analyze(code)
        except Exception:
            data = None
        if data:
            data_context = f"""
以下是 {name} ({code}) 的最新量化分析數據（來自我們的 LightGBM 模型與技術指標系統）：
{_build_single_stock_context(data)}
- 回測結論：{data.get('bt', {}).get('conclusion', '無')}

請根據以上「真實數據」來回答使用者的問題。數據是核心依據，你的角色是用白話文幫新手解讀這些數據。
"""
    # 2. If no individual stock, try sector/industry match
    if not data_context:
        cat, cat_codes = _match_sector_from_prompt(prompt)
        if cat and cat_codes:
            sector_data = _gather_sector_data(cat_codes)
            if sector_data:
                stock_lines = "\n".join(
                    _build_single_stock_context(d) for _, d in sector_data
                )
                avg_prob = sum(d["prob"] for _, d in sector_data) / len(sector_data)
                bullish = sum(1 for _, d in sector_data if d["trend"] == "多頭")
                total = len(sector_data)
                data_context = f"""
以下是「{cat}」產業的量化分析數據（來自我們的 LightGBM 模型，共掃描 {total} 檔代表性個股）：

產業概覽：
- 平均 AI 五日上漲機率：{avg_prob:.0f}%
- 多頭比例：{bullish}/{total} 檔呈多頭趨勢
- {'產業整體偏多' if bullish > total / 2 else '產業整體偏空' if bullish < total / 2 else '產業多空分歧'}

個股明細：
{stock_lines}

請根據以上「真實數據」綜合分析該產業的整體狀態與投資方向。引用具體個股數據來支撐你的論點，幫新手理解產業全貌。
"""
    if not data_context:
        data_context = _build_papi_sector_examples()

    return f"""你是 Papi，也知道自己是 AI。

Papi 取自法文 papillon 的品牌化縮寫，意思是「蝴蝶」。你的品牌意象不是可愛，也不是童話感，而是敏銳、輕盈、能捕捉市場轉折訊號。你像一個在市場資料中快速穿梭的觀察者，專門從雜訊裡辨識趨勢變化、風險升高與可能的觀察機會。

你的任務不是聊天，而是替 LINE bot 使用者快速整理台股研究摘要與投資分析，讓使用者知道「現在能不能看、訊號在哪裡、風險有沒有變大」。

品牌核心：
* Papi 不是預言市場的角色，而是幫使用者從資料雜訊中辨識訊號的市場觀察者。
* 你重視的是「訊號是否清楚」、「風險是否升高」、「現在是否值得觀察」，而不是催促使用者買賣。
* 你不保證市場方向，只根據目前資料判斷機率、趨勢與風險。
* 蝴蝶意象應該體體現在分析方式，而不是每次回答都直接提到蝴蝶。

身份與定位：
* 你負責替 LINE bot 使用者做台股研究摘要與投資分析。
* 你的分析奠基於 LightGBM 量化模型與技術指標系統產出的真實數據。
* 你要把模型、技術指標、外資資料與產業預測，翻成新手聽得懂的判斷。
* 你的重點不是給投資口號，而是幫使用者快速知道目前是「可觀察」、「先等等」、「風險偏高」還是「資料不足」。
* 你可以判斷趨勢偏多、偏空或中性，但不能把模型結果說成保證。

品牌人格與風格限制：
* 你的語氣冷靜、簡潔、敏銳。不說教，也不推銷。
* 嚴格禁止使用任何無關的日常生活比喻（如跑車、雨天、購物等非財經事物比喻）。請直接使用單純、精確的白話財經語意說明。
* 該潑冷水就潑，但一定要說清楚原因。

指標新手翻譯對照表：
* AI 勝率 (prob)：AI 預測「未來 5 個交易日上漲的機率」。>58% 代表短線動能偏多；<45% 代表短線動能極弱。
* RSI 強弱指標 (rsi)：>70 視為「短線買氣超買過熱，追高風險升高」；<30 視為「超賣，可能醞釀反彈」。
* KD 隨機指標：黃金交叉為「短線價格轉折向上，是止跌或發動的初期訊號」；死亡交叉為「短線價格轉折向下，動能轉弱」。
* MACD 柱體：紅柱為「多頭氣勢擴大，價格容易續強」；綠柱為「多頭動能減弱或空頭修正中」。
* 外資買賣超/5日淨額：外資代表法人大戶資金。正值且大代表大戶買超，股價支撐力較強；負值代表大戶賣超，散戶接盤。
* 回測策略報酬/夏普值：模型歷史回測的表現。夏普值 > 1.5 代表該策略歷史走勢非常穩健，波動度較可控。

多指標決策優先順序（由高至低）：
1. 第一優先（風險煞車）：只要 RSI > 70（超買過熱）或 KD 出現死亡交叉，無論 AI 勝率多高，一律判定為「風險偏高」或「先等等」，並警告追高風險。
2. 第二優先（大戶避險）：若 AI 勝率偏多（>58%），但外資5日淨額為負（大戶賣超），必須判定為「先等等」，提醒新手雖有動能但大戶在撤退。
3. 第三優先（同向支持）：AI 勝率偏多（>58%）＋ KD 黃金交叉 ＋ MACD 紅柱 ＋ 外資買超，可判定為「可觀察」。

回答格式：
* 使用繁體中文與全形標點。
* 回覆可分成 2 到 3 段，每段 1 到 2 句。
* 第一段先講核心結論，最好直接落在「可觀察」、「先等等」、「風險偏高」或「資料不足」其中一種狀態。
* 第二段用具體的數據與白話翻譯支撐，切勿含糊。例如：「RSI 72（進入短線超買區）、外資5日賣超 1200 張（大戶退場）。」
* 第三段只在需要時提醒新手下一步的具體觀察指標或風險。
* 不需要寫標題，邏輯順序必須是：結論 → 依據 → 風險或觀察重點。
* 結尾或說明時，請明確提醒這只是「1~2 週的短線波段參考」，非長期投資建議。
* 如果使用者問「有什麼可以觀察、推薦、挑哪幾檔」時，最多提出 2 到 3 檔，且必須來自提供的產業預測或個股數據。

常用語氣與回答範例：
* 「先等等，目前訊號還不夠乾淨。雖然 AI 預測未來 5 天上漲機率有 62%（偏多），但 RSI 已經來到 72（進入短線超買區），追高風險相對升高。
新手建議在場外觀察，等 RSI 降溫、KD 重新出現黃金交叉再做判斷。（本分析為 1~2 週短線波段參考）」

* 「可觀察。AI 勝率 61%（短線動能偏多），且 KD 出現黃金交叉（短線價格轉折向上），外資近 5 日買超 2500 張代表有大戶資金支持。
這裡要注意外資是否持續買超，如果轉為賣超，短線動能可能會減弱。（本分析為 1~2 週短線波段參考）」

{data_context}

使用者問題：{prompt}"""


def call_papi_gemini_fallback(prompt):
    if not gemini_model:
        return None
    safety = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    response = gemini_model.generate_content(_build_papi_prompt(prompt), safety_settings=safety)
    summary = (getattr(response, "text", "") or "").strip()
    if not summary:
        return None
    return summary


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
    scan_limit = max(SECTOR_SCAN_LIMIT, SECTOR_DISPLAY_LIMIT * 6)
    for category, codes in market_map.items():
        items = []
        for code in sector_candidates(category, codes, limit=scan_limit):
            try:
                item = sector_signal_item(code, analyze_fn(code))
            except Exception:
                item = None
            if item:
                items.append(item)
                dates.append(item["as_of"])
                if len(items) >= SECTOR_DISPLAY_LIMIT:
                    break
        items.sort(key=lambda item: item["score"], reverse=True)
        sectors[category] = items
    return {
        "as_of": max(dates) if dates else now.date().isoformat(),
        "generated_at": now.replace(microsecond=0).isoformat() + "Z",
        "sectors": sectors,
    }


def build_market_map():
    market = {"全市場": [], "ETF專區": []}
    for theme in PAPI_THEME_SECTORS:
        market[theme] = []
    for code, info in twstock.codes.items():
        if len(code) not in [4, 5]: continue
        grp = getattr(info, "group", None) or getattr(info, "type", None)
        if grp and isinstance(grp, str) and grp.strip():
            market["全市場"].append(code)
            if code.startswith("00"): market["ETF專區"].append(code)
            for theme, names in PAPI_THEME_SECTORS.items():
                if info.name in names and code not in market[theme]:
                    market[theme].append(code)
    return {k: v for k, v in market.items() if v}

industry_map = build_market_map()

def build_category_quick_reply(page=1):
    cats = list(industry_map.keys())
    total = 1 if not cats else (len(cats) + CATEGORY_PAGE_SIZE - 1) // CATEGORY_PAGE_SIZE
    page = max(1, min(page, total))
    start = (page - 1) * CATEGORY_PAGE_SIZE
    items = [QuickReplyButton(action=MessageAction(label=c[:20], text=f"選產業_{c}")) for c in cats[start:start + CATEGORY_PAGE_SIZE]]
    if page < total and len(items) < 13:
        items.append(QuickReplyButton(action=MessageAction(label="更多分類▶", text=f"分類第_{page + 1}頁")))
    return QuickReply(items=items), f"請選擇市場類別（第 {page}/{total} 頁）👇"

# ==================================================
# 7. 自動化發報引擎
# ==================================================
@app.route("/broadcast_weekly", methods=["GET"])
def broadcast_weekly():
    if not BROADCAST_TOKEN:
        return "廣播功能未設定", 503
    if not hmac.compare_digest(request.args.get("token", ""), BROADCAST_TOKEN):
        return "身份驗證失敗", 403
    d = analyze("TAIEX")
    if not d: return "分析失敗", 500
    
    insight = get_ai_insight_for_broadcast("台股大盤", {"price": d['price'], "prob": d['prob']}, d['bt'], d['news'])
    
    url = f"{request.host_url}market".replace("http://", "https://")
    msg = f"🌞 周一 AI 投資晨報\n\n📊 大盤分析：\n{insight}\n\n🔗 點擊查看 AI 預測軌跡：\n{url}"
    try:
        line_bot_api.broadcast(TextSendMessage(text=msg))
        return f"廣播成功：{datetime.datetime.now()}", 200
    except Exception as e:
        return f"發送失敗：{str(e)}", 500

# ==================================================
# 8. 路由與 LINE 基礎指令 (💡 確保名稱不重複版)
# ==================================================
def get_line_state(user_id):
    if line_store is None:
        raise StoreError("關注功能尚未設定")
    return line_store.load(user_id)[0]


def get_line_state_bounded(user_id, timeout=LINE_STATE_READ_BUDGET_SECONDS):
    store = line_store
    if store is None:
        raise StoreError("關注功能尚未設定")
    result = queue.Queue(maxsize=1)
    slots = _line_state_read_slots
    if not slots.acquire(blocking=False):
        raise StoreError("關注功能讀取忙碌")

    def load_state():
        try:
            value = (False, None)
            try:
                value = (True, store.load(user_id)[0])
            except BaseException:
                pass
            try:
                result.put_nowait(value)
            except BaseException:
                pass
        finally:
            slots.release()

    try:
        threading.Thread(target=load_state, daemon=True).start()
    except BaseException as error:
        slots.release()
        if isinstance(error, Exception):
            raise StoreError("關注功能讀取失敗") from None
        raise
    try:
        succeeded, state = result.get(timeout=timeout)
    except queue.Empty:
        raise StoreError("關注功能讀取逾時") from None
    if not succeeded:
        raise StoreError("關注功能讀取失敗")
    return state


def update_line_state(user_id, mutate):
    if line_store is None:
        raise StoreError("關注功能尚未設定")
    return line_store.update(user_id, mutate)


def _store_error_text():
    if line_store is None:
        return "關注功能尚未設定，請稍後再試。"
    return "關注功能暫時無法使用，請稍後再試。"


def build_stock_flex_message(code, name, data, url, watched=False):
    color_prob = "#10b981" if data['prob'] >= 50 else "#ef4444"
    color_s = "#10b981" if data['s_score'] >= 50 else "#ef4444"
    color_trend = "#10b981" if "多" in data['trend'] else "#ef4444"
    sentiment_summary = (
        f"{data.get('news_count', len(data.get('news', [])))} 則｜"
        f"正面 {round(data.get('news_positive_ratio', 0) * 100)}%｜"
        f"負面 {round(data.get('news_negative_ratio', 0) * 100)}%｜"
        f"可信度{data.get('news_confidence', '低')}"
    )

    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1e293b",
            "paddingAll": "20px",
            "contents": [
                {
                    "type": "text",
                    "text": f"📊 {name} ({code})",
                    "color": "#ffffff",
                    "weight": "bold",
                    "size": "xl"
                }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#f8fafc",
            "paddingAll": "20px",
            "spacing": "md",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        { "type": "text", "text": "💰 最新收盤", "color": "#64748b", "size": "sm", "flex": 4 },
                        { "type": "text", "text": f"{data['price']:.2f}", "color": "#0f172a", "size": "md", "weight": "bold", "align": "end", "flex": 5 }
                    ]
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        { "type": "text", "text": "📈 當前趨勢", "color": "#64748b", "size": "sm", "flex": 4 },
                        { "type": "text", "text": data['trend'], "color": color_trend, "size": "md", "weight": "bold", "align": "end", "flex": 5 }
                    ]
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        { "type": "text", "text": "🌡 新聞情緒", "color": "#64748b", "size": "sm", "flex": 4 },
                        { "type": "text", "text": f"{data['s_status']} ({data['s_score']:.1f})", "color": color_s, "size": "md", "weight": "bold", "align": "end", "flex": 5 }
                    ]
                },
                {
                    "type": "text",
                    "text": sentiment_summary,
                    "color": "#64748b",
                    "size": "xs",
                    "align": "end",
                    "wrap": True
                },
                { "type": "separator", "margin": "md", "color": "#cbd5e1" },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "margin": "md",
                    "contents": [
                        { "type": "text", "text": "🎯 五日上漲機率", "color": "#0f172a", "size": "md", "weight": "bold", "flex": 4 },
                        { "type": "text", "text": f"{data['prob']}%", "color": color_prob, "size": "lg", "weight": "bold", "align": "end", "flex": 5 }
                    ]
                }
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#f8fafc",
            "paddingAll": "16px",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {
                        "type": "postback",
                        "label": "移除關注" if watched else "加入關注",
                        "data": f"watch:{'remove' if watched else 'add'}:{code}",
                    }
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {
                        "type": "postback",
                        "label": "設定提醒",
                        "data": f"alert:menu:{code}",
                    }
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {
                        "type": "postback",
                        "label": "投資試算",
                        "data": f"calc:menu:{code}",
                    }
                },
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#39c6a3",
                    "action": {
                        "type": "uri",
                        "label": "查看完整分析",
                        "uri": url,
                    }
                }
            ]
        }
    }


def _empty_line_bubble(title, description):
    return {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "20px",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": title, "weight": "bold", "size": "lg", "wrap": True},
                {"type": "text", "text": description, "color": "#64748b", "size": "sm", "wrap": True},
            ],
        },
    }


def _watchlist_card(item, snapshot, base_url):
    code = item["code"]
    name = item["name"]
    if snapshot:
        details = [
            f"收盤價 {snapshot['price']:.2f}",
            f"五日上漲機率 {snapshot['prob']}%",
            f"趨勢 {snapshot['trend']}",
            f"資料日期 {snapshot['as_of']}",
        ]
    else:
        details = ["待收盤更新"]
    return {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "18px", "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"{name} ({code})", "weight": "bold", "size": "lg", "wrap": True},
                *[
                    {"type": "text", "text": detail, "color": "#64748b", "size": "sm", "wrap": True}
                    for detail in details
                ],
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "14px", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "secondary", "action": {
                    "type": "postback", "label": "移除關注", "data": f"watch:remove:{code}",
                }},
                {"type": "button", "style": "secondary", "action": {
                    "type": "postback", "label": "設定提醒", "data": f"alert:menu:{code}",
                }},
                {"type": "button", "style": "primary", "color": "#39c6a3", "action": {
                    "type": "uri", "label": "查看完整分析",
                    "uri": f"{base_url.rstrip('/')}/stock/{code}",
                }},
            ],
        },
    }


def build_watchlist_flex(state, base_url):
    watchlist = state.get("watchlist", [])[:12]
    if not watchlist:
        return _empty_line_bubble("我的關注", "尚未加入關注股票。請先查詢個股，再點選「加入關注」。")
    snapshots = {
        item.get("code"): item
        for item in state.get("signals", {}).get("items", [])
        if isinstance(item, dict)
    }
    return {
        "type": "carousel",
        "contents": [
            _watchlist_card(item, snapshots.get(item.get("code")), base_url)
            for item in watchlist
        ],
    }


def _alert_condition_text(alert):
    if alert["kind"] in {"price", "price_above"}:
        return f"收盤價站上 {float(alert['value']):g}"
    if alert["kind"] == "price_below":
        return f"收盤價跌破 {float(alert['value']):g}"
    if alert["kind"] == "probability":
        return f"AI 勝率達到 {float(alert['value']):g}%"
    return f"趨勢為{alert['value']}"


def _alert_management_card(alert):
    return {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "18px", "spacing": "sm",
            "contents": [
                {"type": "text", "text": "提醒管理", "weight": "bold", "size": "sm", "color": "#39c6a3"},
                {"type": "text", "text": f"{alert['name']} ({alert['code']})", "weight": "bold", "size": "lg", "wrap": True},
                {"type": "text", "text": _alert_condition_text(alert), "color": "#64748b", "size": "sm", "wrap": True},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "14px",
            "contents": [{"type": "button", "style": "secondary", "action": {
                "type": "postback", "label": "取消提醒", "data": f"alert:remove:{alert['id']}",
            }}],
        },
    }


def build_alerts_flex(state):
    alerts = [alert for alert in state.get("alerts", []) if alert.get("enabled", True)][:12]
    if not alerts:
        return _empty_line_bubble("提醒管理", "尚未設定提醒。請先查詢個股，再點選「設定提醒」。")
    return {"type": "carousel", "contents": [_alert_management_card(alert) for alert in alerts]}


def build_alert_menu_flex(code, name):
    choices = [
        ("站上收盤價", f"alert:start:{code}:price_above"),
        ("跌破收盤價", f"alert:start:{code}:price_below"),
        ("AI 勝率門檻", f"alert:start:{code}:probability"),
        ("趨勢為多頭", f"alert:trend:{code}:多頭"),
        ("趨勢為空頭", f"alert:trend:{code}:空頭"),
    ]
    return {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "18px", "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"設定 {name} ({code}) 提醒", "weight": "bold", "size": "lg", "wrap": True},
                *[
                    {"type": "button", "style": "secondary", "action": {
                        "type": "postback", "label": label, "data": payload,
                    }}
                    for label, payload in choices
                ],
            ],
        },
    }


def build_calculator_menu_flex(code, name):
    choices = [("1 萬", 10000), ("5 萬", 50000), ("10 萬", 100000)]
    return {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "18px", "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"{name} 投資試算", "weight": "bold", "size": "lg", "wrap": True},
                {"type": "text", "text": "請選擇投入金額，或點自訂金額查看輸入格式。", "color": "#64748b", "size": "sm", "wrap": True},
                *[
                    {"type": "button", "style": "secondary", "action": {
                        "type": "postback", "label": label, "data": f"calc:amount:{code}:{amount}",
                    }}
                    for label, amount in choices
                ],
                {"type": "button", "style": "secondary", "action": {
                    "type": "postback", "label": "自訂金額", "data": f"calc:custom:{code}",
                }},
            ],
        },
    }


def build_projection_flex(code, name, data, amount, base_url):
    projection = calculate_investment_projection(amount, data)
    if not projection["ok"]:
        return _empty_line_bubble("投資試算", "金額不足買進 1 股，請提高投入金額後再試。")
    return {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "18px", "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"{name} ({code})", "weight": "bold", "size": "lg", "wrap": True},
                {"type": "text", "text": f"投入 {projection['amount']:,.0f} 元，約可買 {projection['shares']:,} 股。", "color": "#0f766e", "weight": "bold", "size": "sm", "wrap": True},
                {"type": "text", "text": f"AI 策略歷史估算損益：{projection['strategy_profit']:,.0f} 元", "color": "#64748b", "size": "sm", "wrap": True},
                {"type": "text", "text": f"買進持有歷史估算損益：{projection['buy_hold_profit']:,.0f} 元", "color": "#64748b", "size": "sm", "wrap": True},
                {"type": "text", "text": "這是歷史回測換算，不代表未來獲利。", "color": "#94a3b8", "size": "xs", "wrap": True},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "14px",
            "contents": [{"type": "button", "style": "primary", "color": "#39c6a3", "action": {
                "type": "uri", "label": "查看完整分析",
                "uri": f"{base_url.rstrip('/')}/stock/{code}",
            }}],
        },
    }


def _signal_card(item, base_url):
    code = item["code"]
    return {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "18px", "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"{item['name']} ({code})", "weight": "bold", "size": "lg", "wrap": True},
                {"type": "text", "text": f"收盤價 {item['price']:.2f}", "color": "#64748b", "size": "sm"},
                {"type": "text", "text": f"五日上漲機率 {item['prob']}%", "color": "#64748b", "size": "sm"},
                {"type": "text", "text": f"趨勢 {item['trend']}", "color": "#64748b", "size": "sm"},
                {"type": "text", "text": f"資料日期 {item['as_of']}", "color": "#64748b", "size": "sm"},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "14px",
            "contents": [{"type": "button", "style": "primary", "color": "#39c6a3", "action": {
                "type": "uri", "label": "查看完整分析",
                "uri": f"{base_url.rstrip('/')}/stock/{code}",
            }}],
        },
    }


def build_strong_signals_flex(state, base_url):
    items = state.get("signals", {}).get("items", [])[:5]
    if not items:
        return _empty_line_bubble("強勢訊號", "尚無最新強勢訊號，請等待下一次收盤更新。")
    return {
        "type": "carousel",
        "contents": [_signal_card(item, base_url) for item in items],
    }


def build_alert_push_flex(hits, base_url):
    if not 1 <= len(hits) <= 12:
        raise ValueError("LINE Flex carousel requires 1 to 12 bubbles")

    def bubble(hit):
        alert, quote = hit["alert"], hit["quote"]
        if alert["kind"] in {"price", "price_above"}:
            condition = f"條件：收盤價站上 {float(alert['value']):g}"
            current = f"今日收盤價：{quote['price']:.2f}"
        elif alert["kind"] == "price_below":
            condition = f"條件：收盤價跌破 {float(alert['value']):g}"
            current = f"今日收盤價：{quote['price']:.2f}"
        elif alert["kind"] == "probability":
            condition = f"條件：AI 勝率達到 {float(alert['value']):g}%"
            current = f"目前 AI 勝率：{quote['prob']}%"
        else:
            condition = f"條件：趨勢為{alert['value']}"
            current = f"目前趨勢：{quote['trend']}"
        return {
            "type": "bubble", "size": "kilo",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#081321",
                "paddingAll": "16px", "contents": [{
                    "type": "text", "text": "🔔 股票提醒", "color": "#39c6a3",
                    "weight": "bold", "size": "sm",
                }],
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "18px", "spacing": "sm",
                "contents": [
                    {"type": "text", "text": f"{quote['name']} ({quote['code']})", "weight": "bold", "size": "lg", "wrap": True},
                    {"type": "text", "text": condition, "size": "sm", "color": "#64748b", "wrap": True},
                    {"type": "text", "text": current, "size": "sm", "color": "#0f766e", "weight": "bold", "wrap": True},
                    {"type": "text", "text": f"資料日期：{quote['as_of']}", "size": "xs", "color": "#94a3b8"},
                ],
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "14px",
                "contents": [{"type": "button", "style": "primary", "color": "#39c6a3", "action": {
                    "type": "uri", "label": "查看完整分析",
                    "uri": f"{base_url.rstrip('/')}/stock/{quote['code']}",
                }}],
            },
        }

    return {"type": "carousel", "contents": [bubble(hit) for hit in hits]}


def run_alert_checks(store, analyze_fn, push_fn, today, base_url):
    quotes = {}
    push_failed = False

    def quote_for(code):
        if code in quotes:
            return quotes[code]
        try:
            data = analyze_fn(code)
            if not data or not isinstance(data.get("as_of"), str):
                quotes[code] = None
                return None
            datetime.date.fromisoformat(data["as_of"])
            quotes[code] = {
                "code": code, "name": data["name"], "price": float(data["price"]),
                "prob": int(data["prob"]), "trend": data["trend"], "as_of": data["as_of"],
            }
        except Exception:
            quotes[code] = None
        return quotes[code]

    for user_id, observed, _ in store.iter_users():
        observed_codes = [
            item["code"] for item in observed.get("watchlist", [])
            if isinstance(item, dict) and item.get("code")
        ]
        watched = [
            quote for code in observed_codes
            for quote in [quote_for(code)]
            if quote is not None
        ]
        if not watched:
            continue
        prior_as_of = {
            item.get("code"): item.get("as_of")
            for item in observed.get("signals", {}).get("items", [])
            if isinstance(item, dict) and item.get("code") and isinstance(item.get("as_of"), str)
        }
        fresh_codes = {
            quote["code"] for quote in watched
            if not prior_as_of.get(quote["code"]) or quote["as_of"] > prior_as_of[quote["code"]]
        }
        if not fresh_codes:
            continue

        latest_as_of = max(item["as_of"] for item in watched)
        signal_items = top_signals(watched)
        hits = []
        for alert in observed.get("alerts", []):
            quote = quotes.get(alert.get("code"))
            if (
                not alert.get("enabled")
                or alert.get("last_triggered_date") == today
                or quote is None
                or quote["code"] not in fresh_codes
            ):
                continue
            try:
                if evaluate_alert(alert, quote):
                    hits.append({"alert": alert, "quote": quote})
            except (KeyError, TypeError, ValueError):
                continue

        if hits:
            messages = [
                build_alert_push_flex(hits[start:start + 12], base_url)
                for start in range(0, len(hits), 12)
            ]
            try:
                push_fn(user_id, messages[0] if len(messages) == 1 else messages)
            except Exception:
                push_failed = True
                continue
        triggered_ids = {hit["alert"]["id"] for hit in hits}

        def merge_scheduler_fields(state):
            current_codes = [
                item["code"] for item in state.get("watchlist", [])
                if isinstance(item, dict) and item.get("code")
            ]
            if current_codes == observed_codes:
                state["signals"] = {
                    "as_of": latest_as_of,
                    "items": [dict(item) for item in signal_items],
                }
            for alert in state.get("alerts", []):
                if alert.get("id") in triggered_ids:
                    alert["last_triggered_date"] = today

        store.update(user_id, merge_scheduler_fields)
    if push_failed:
        raise RuntimeError("部分 LINE 提醒發送失敗")


def _system_document_url(store, document_id):
    return (
        "https://firestore.googleapis.com/v1/projects/"
        f"{store.project_id}/databases/(default)/documents/system/"
        f"{urllib.parse.quote(document_id, safe='')}"
    )


def save_sector_signal_snapshot(store, snapshot):
    body = {
        "fields": {
            "payload": {
                "stringValue": json.dumps(
                    snapshot, ensure_ascii=False, separators=(",", ":")
                )
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
        raise StoreError(
            f"sector snapshot write failed with status {response.status_code}"
        )


def load_sector_signal_snapshot(store):
    response = store._request(
        "GET", _system_document_url(store, SECTOR_SNAPSHOT_DOC), timeout=5
    )
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise StoreError(
            f"sector snapshot read failed with status {response.status_code}"
        )
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


def build_line_summary_card(title, lines, cta_label, url, accent="#39c6a3", action=None):
    """建立只有一個主要動作的 LINE 摘要卡。"""
    action = action or {"type": "uri", "label": cta_label, "uri": url}
    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#081321",
            "paddingAll": "16px", "contents": [{
                "type": "text", "text": "AI QUANT", "color": accent,
                "size": "xs", "weight": "bold",
            }],
        },
        "body": {
            "type": "box", "layout": "vertical", "backgroundColor": "#0d1a2b",
            "paddingAll": "18px", "spacing": "md", "contents": [
                {"type": "text", "text": title, "color": "#eef6ff", "size": "lg", "weight": "bold", "wrap": True},
                *[{"type": "text", "text": line, "color": "#8fa4bd", "size": "sm", "wrap": True} for line in lines],
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "backgroundColor": "#0d1a2b",
            "paddingAll": "14px", "contents": [{
                "type": "button", "style": "primary", "color": accent,
                "action": action,
            }],
        },
    }

def build_line_navigation_flex(base_url):
    """Rich Menu 入口的可預覽 Flex 版本。"""
    root = base_url.rstrip("/")
    entries = [
        ("看大盤", "今天盤面偏強還是偏弱", "查看盤勢", {"type": "uri", "label": "查看盤勢", "uri": f"{root}/market"}),
        ("找機會", "產業預測與熱門題材", "選擇產業", {"type": "message", "label": "選擇產業", "text": "預測"}),
        ("查自選", "自選股票清單", "開啟關注", {"type": "message", "label": "開啟關注", "text": "我的關注"}),
        ("設提醒", "收盤與趨勢通知", "管理提醒", {"type": "message", "label": "管理提醒", "text": "提醒管理"}),
        ("算報酬", "投入金額試算", "開始試算", {"type": "message", "label": "開始試算", "text": "投資試算"}),
        ("深度分析", "圖表、回測、新聞", "開啟分析", {"type": "uri", "label": "開啟分析", "uri": f"{root}/dashboard"}),
    ]
    return {
        "type": "carousel",
        "contents": [build_line_summary_card(title, [description], cta, root, action=action) for title, description, cta, action in entries],
    }

def build_calculator_help_flex():
    return build_line_summary_card(
        "投資試算",
        [
            "先輸入股票代碼，例如 2330。",
            "查詢結果會出現「投資試算」按鈕，可直接選 1 萬 / 5 萬 / 10 萬。",
            "自訂金額可輸入：試算 2330 100000",
        ],
        "輸入 2330 開始",
        "2330",
        action={"type": "message", "label": "輸入 2330 開始", "text": "2330"},
    )

def build_welcome_flex():
    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#0f172a",
            "paddingAll": "20px",
            "contents": [
                { "type": "text", "text": "🤖 AI 選股助理", "color": "#38bdf8", "weight": "bold", "size": "xl" }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1e293b",
            "paddingAll": "20px",
            "spacing": "md",
            "contents": [
                { "type": "text", "text": "歡迎使用 AI 量化投資預測！", "color": "#f8fafc", "size": "md", "weight": "bold", "wrap": True },
                { "type": "text", "text": "您可以：\n1️⃣ 點擊下方選單選擇有興趣的【產業】\n2️⃣ 直接輸入【股票代碼】(如 2330)\n3️⃣ 輸入【大盤】查看今日走勢", "color": "#94a3b8", "size": "sm", "wrap": True, "margin": "md" }
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1e293b",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "button",
                    "style": "secondary",
                    "color": "#334155",
                    "action": { "type": "message", "label": "🎓 新手怎麼看？(教學)", "text": "新手教學" }
                }
            ]
        }
    }

def build_tutorial_flex():
    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#6366f1",
            "paddingAll": "20px",
            "contents": [
                { "type": "text", "text": "🎓 新手快速上手指南", "color": "#ffffff", "weight": "bold", "size": "xl" }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#f8fafc",
            "paddingAll": "20px",
            "spacing": "md",
            "contents": [
                { "type": "text", "text": "不用擔心看不懂複雜的數據，只要掌握以下三個重點：", "color": "#475569", "size": "sm", "wrap": True, "weight": "bold", "margin": "sm" },
                { "type": "separator", "margin": "md", "color": "#cbd5e1" },
                { "type": "text", "text": "🎯 1. 看「五日上漲機率」", "color": "#0f172a", "size": "md", "weight": "bold", "margin": "md" },
                { "type": "text", "text": "AI 會根據過去的數據估計五個交易日後上漲的機率。大於 60% 代表機率偏高（綠字），低於 40% 建議保守觀望（紅字）。", "color": "#64748b", "size": "sm", "wrap": True },
                { "type": "text", "text": "🌡 2. 看「新聞情緒」", "color": "#0f172a", "size": "md", "weight": "bold", "margin": "md" },
                { "type": "text", "text": "我們會自動分析最近的新聞是利多還利空。「樂觀貪婪」代表市場氣氛好，「悲觀恐慌」代表市場害怕。", "color": "#64748b", "size": "sm", "wrap": True },
                { "type": "text", "text": "📖 3. 專有名詞看不懂？", "color": "#0f172a", "size": "md", "weight": "bold", "margin": "md" },
                { "type": "text", "text": "直接點擊個股的「📈 查看圖表與回測報告」，滑到網頁最下方，就有白話文的【新手投資小辭典】幫你翻譯各種專業術語喔！", "color": "#64748b", "size": "sm", "wrap": True }
            ]
        }
    }

def _build_stock_row(code):
    name = get_stock_name(code)
    return {
        "type": "box",
        "layout": "horizontal",
        "paddingAll": "12px",
        "cornerRadius": "8px",
        "backgroundColor": "#ffffff",
        "spacing": "sm",
        "margin": "md",
        "action": { "type": "message", "label": f"查詢 {code}", "text": code },
        "contents": [
            { "type": "text", "text": f"{code}", "color": "#64748b", "size": "sm", "weight": "bold", "flex": 2 },
            { "type": "text", "text": f"{name}", "color": "#0f172a", "size": "md", "weight": "bold", "flex": 4 },
            { "type": "text", "text": "前往分析 ▶", "color": "#0284c7", "size": "xs", "align": "end", "gravity": "center", "flex": 3 }
        ]
    }

def build_industry_carousel(cat, arr):
    bubbles = []
    aggr_list = arr[:5]
    if aggr_list:
        bubbles.append({
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#ef4444",
                "paddingAll": "16px",
                "contents": [ { "type": "text", "text": f"🔥 {cat} | 激進型推薦", "color": "#ffffff", "weight": "bold", "size": "lg" } ]
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#f8fafc",
                "paddingAll": "12px",
                "contents": [_build_stock_row(c) for c in aggr_list]
            }
        })
    cons_list = arr[5:10]
    if cons_list:
        bubbles.append({
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#3b82f6",
                "paddingAll": "16px",
                "contents": [ { "type": "text", "text": f"🛡️ {cat} | 保守型推薦", "color": "#ffffff", "weight": "bold", "size": "lg" } ]
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#f8fafc",
                "paddingAll": "12px",
                "contents": [_build_stock_row(c) for c in cons_list]
            }
        })
    return { "type": "carousel", "contents": bubbles }


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
            {
                "type": "text", "text": f"{name} ({code})",
                "color": "#0f172a", "size": "md", "weight": "bold", "wrap": True,
            },
            {
                "type": "text",
                "text": f"AI勝率 {item['prob']}%｜{item['trend']}｜外資5日 {item['foreign_net_5']:,.0f}",
                "color": "#475569", "size": "xs", "wrap": True,
            },
            {
                "type": "text",
                "text": f"排序分數 {item['score']:.1f}｜資料 {item['as_of']}",
                "color": "#0284c7", "size": "xs", "wrap": True,
            },
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
            "contents": [{
                "type": "text", "text": f"📊 {category}｜每日產業預測",
                "color": "#ffffff", "weight": "bold", "size": "lg", "wrap": True,
            }],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#f8fafc",
            "paddingAll": "12px",
            "contents": [
                _build_sector_signal_row(item)
                for item in items[:SECTOR_DISPLAY_LIMIT]
            ],
        },
    }


@app.route("/")
def home():
    """健康檢查端點：給外部監控服務敲擊，防止 Render 休眠"""
    return "AI Stock Bot is awake and running!", 200

@app.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html")

@app.route("/watchlist")
def watchlist_page():
    return redirect("/dashboard", code=302)

@app.route("/api/dashboard")
def dashboard_api():
    market = analyze("TAIEX")
    if not market:
        return jsonify({"error": "market data unavailable"}), 503
    sector_cards = dashboard_sector_cards()
    sectors = [
        {"name": name, "count": len(codes)}
        for name, codes in list(industry_map.items())[:8]
    ]
    return jsonify({
        "market": {
            "price": float(market["price"]),
            "prob": int(market["prob"]),
            "trend": market["trend"],
        },
        "opportunities": cached_opportunities(),
        "sector_cards": sector_cards,
        "top_picks": dashboard_top_picks(sector_cards),
        "watchlist_hint": {
            "title": "關注與提醒在 LINE 管理",
            "steps": ["在 LINE 查詢個股", "點選加入關注", "從提醒管理設定通知"],
        },
        "sectors": sectors,
    })

@app.route("/stock/<code>")
def stock_page(code):
    if code not in twstock.codes:
        abort(404)
    d = analyze(code)
    return render_template("stock_detail.html", d=d) if d else "查無資料"

@app.route("/market")
def market_page():
    d = analyze("TAIEX")
    return render_template("stock_detail.html", d=d) if d else "資料更新中"

@app.route("/callback", methods=["POST"])
def callback():
    try: handler.handle(request.get_data(as_text=True), request.headers.get("X-Line-Signature", ""))
    except InvalidSignatureError: abort(400)
    return "OK"


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


@app.route("/tasks/check-alerts", methods=["POST"])
def check_alerts_task():
    if not ALERT_TASK_TOKEN:
        return "提醒排程尚未設定", 503
    if not hmac.compare_digest(
        request.headers.get("Authorization", ""),
        f"Bearer {ALERT_TASK_TOKEN}",
    ):
        return "身份驗證失敗", 403
    if line_store is None:
        return "關注功能尚未設定", 503

    def push(user_id, contents):
        messages = contents if isinstance(contents, list) else [contents]
        messages = [
            FlexSendMessage(alt_text="股票提醒已觸發", contents=message)
            for message in messages
        ]
        line_bot_api.push_message(user_id, messages[0] if len(messages) == 1 else messages)

    try:
        run_alert_checks(
            line_store,
            analyze,
            push,
            datetime.date.today().isoformat(),
            request.host_url.replace("http://", "https://").rstrip("/"),
        )
    except Exception:
        return "提醒排程執行失敗", 500
    return "提醒排程執行完成", 200


def _reply_text(event, text):
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))


def _current_web_root():
    return request.host_url.replace("http://", "https://").rstrip("/")


def _require_same_pending(state, expected_pending):
    if state.get("pending") != expected_pending:
        raise StateError("提醒設定已變更，請重新操作。")


def _find_matching_alert(alerts, code, kind, value):
    return next(
        (
            alert for alert in alerts
            if alert.get("code") == code
            and (
                alert.get("kind") == kind
                or {alert.get("kind"), kind} <= {"price", "price_above"}
            )
            and alert.get("value") == value
        ),
        None,
    )


def _resolve_postback_stock(code):
    resolved_code, name = search_stock_code(code)
    if (
        resolved_code != code
        or not name
        or (name == code and code != "TAIEX")
    ):
        return None, None
    return resolved_code, name


@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = getattr(getattr(event, "source", None), "user_id", None)
    if not user_id:
        _reply_text(event, "無法識別 LINE 使用者，請從一對一聊天室操作。")
        return

    payload = getattr(getattr(event, "postback", None), "data", "")
    stock_match = re.fullmatch(r"(?:watch:(?:add|remove)|alert:menu|calc:menu|calc:custom):([A-Za-z0-9]+)", payload)
    calc_amount_match = re.fullmatch(r"calc:amount:([A-Za-z0-9]+):([0-9]+(?:\.[0-9]+)?)", payload)
    alert_start_match = re.fullmatch(
        r"alert:start:([A-Za-z0-9]+):(price|price_above|price_below|probability)",
        payload,
    )
    alert_trend_match = re.fullmatch(
        r"alert:trend:([A-Za-z0-9]+):(多頭|空頭)",
        payload,
    )
    alert_remove_match = re.fullmatch(r"alert:remove:([0-9a-fA-F]{32})", payload)

    if not any((stock_match, calc_amount_match, alert_start_match, alert_trend_match, alert_remove_match)):
        _reply_text(event, "無效的操作，請重新開啟功能選單。")
        return

    if alert_remove_match:
        alert_id = alert_remove_match.group(1)
        found = {"value": False}

        def delete_alert(state):
            alerts = state.get("alerts", [])
            found["value"] = any(item.get("id") == alert_id for item in alerts)
            state["alerts"] = [item for item in alerts if item.get("id") != alert_id]

        try:
            update_line_state(user_id, delete_alert)
            _reply_text(event, "提醒已移除。" if found["value"] else "找不到這筆提醒，可能已經移除。")
        except StoreError:
            _reply_text(event, _store_error_text())
        return

    match = stock_match or calc_amount_match or alert_start_match or alert_trend_match
    code, name = _resolve_postback_stock(match.group(1))
    if not code:
        _reply_text(event, "找不到這檔股票，請重新查詢後再操作。")
        return

    if payload == f"alert:menu:{code}":
        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(
                alt_text=f"設定 {name} 提醒",
                contents=build_alert_menu_flex(code, name),
            ),
        )
        return

    if payload == f"calc:menu:{code}":
        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(
                alt_text=f"{name} 投資試算",
                contents=build_calculator_menu_flex(code, name),
            ),
        )
        return

    if payload == f"calc:custom:{code}":
        _reply_text(event, f"請輸入：試算 {code} 100000\n把 100000 換成你的投入金額。")
        return

    if calc_amount_match:
        data = analyze(code)
        if not data:
            _reply_text(event, "查無資料，請稍後再試。")
            return
        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(
                alt_text=f"{name} 投資試算",
                contents=build_projection_flex(code, name, data, calc_amount_match.group(2), _current_web_root()),
            ),
        )
        return

    try:
        if payload == f"watch:add:{code}":
            update_line_state(user_id, lambda state: add_watch(state, code, name))
            reply = f"已將 {name} ({code}) 加入關注。"
        elif payload == f"watch:remove:{code}":
            update_line_state(user_id, lambda state: remove_watch(state, code))
            reply = f"已將 {name} ({code}) 移除關注，相關提醒也已移除。"
        elif alert_start_match:
            kind = alert_start_match.group(2)

            def begin_alert(state):
                add_watch(state, code, name)
                start_pending(state, code, name, kind)

            update_line_state(user_id, begin_alert)
            label = {
                "price": "收盤價站上",
                "price_above": "收盤價站上",
                "price_below": "收盤價跌破",
            }.get(kind, "AI 勝率（1 到 99）")
            reply = f"請輸入 {name} 的{label}門檻數字，或輸入「取消」。"
        elif alert_trend_match:
            trend = alert_trend_match.group(2)
            created = {"value": False}

            def create_trend_alert(state):
                created["value"] = False
                add_watch(state, code, name)
                if _find_matching_alert(state.get("alerts", []), code, "trend", trend):
                    return
                add_alert(state, code, name, "trend", trend)
                created["value"] = True

            update_line_state(user_id, create_trend_alert)
            reply = (
                f"已建立 {name} 趨勢為{trend}時的提醒。"
                if created["value"]
                else f"{name} 趨勢為{trend}時的提醒已存在。"
            )
        else:
            _reply_text(event, "無效的操作，請重新開啟功能選單。")
            return
        _reply_text(event, reply)
    except StateError as error:
        _reply_text(event, str(error))
    except StoreError:
        _reply_text(event, _store_error_text())


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    web_root = request.host_url.replace("http://", "https://").rstrip("/")
    user_id = getattr(getattr(event, "source", None), "user_id", None)
    current_state = None
    state_load_failed = False

    if line_store is not None and user_id:
        try:
            current_state = get_line_state_bounded(user_id)
        except StoreError:
            state_load_failed = True

    if current_state and current_state.get("pending"):
        expected_pending = dict(current_state["pending"])
        try:
            if msg == "取消":
                def cancel_pending(state):
                    _require_same_pending(state, expected_pending)
                    state["pending"] = None

                update_line_state(user_id, cancel_pending)
                _reply_text(event, "已取消提醒設定。")
            else:
                outcome = {"alert": None, "created": False, "expired": False}

                def finish_pending(state):
                    outcome.update(alert=None, created=False, expired=False)
                    _require_same_pending(state, expected_pending)
                    now = time.time()
                    if expected_pending["expires_at"] <= now:
                        state["pending"] = None
                        outcome["expired"] = True
                        return
                    preview_state = {
                        "pending": dict(expected_pending),
                        "alerts": [],
                    }
                    preview = consume_pending(preview_state, msg, now=now)
                    duplicate = _find_matching_alert(
                        state.get("alerts", []),
                        preview["code"],
                        preview["kind"],
                        preview["value"],
                    )
                    if duplicate:
                        state["pending"] = None
                        outcome["alert"] = duplicate
                        return
                    alert = consume_pending(state, msg, now=now)
                    outcome["alert"] = alert
                    outcome["created"] = True

                update_line_state(user_id, finish_pending)
                if outcome["expired"]:
                    _reply_text(event, "提醒設定已逾時，請重新設定。")
                else:
                    alert = outcome["alert"]
                    label = {
                        "price": "收盤價站上",
                        "price_above": "收盤價站上",
                        "price_below": "收盤價跌破",
                    }.get(alert["kind"], "AI 勝率")
                    reply = (
                        f"已建立 {alert['name']} 的{label}提醒。"
                        if outcome["created"]
                        else f"{alert['name']} 的{label}提醒已存在。"
                    )
                    _reply_text(event, reply)
        except StateError as error:
            _reply_text(event, str(error))
        except StoreError:
            _reply_text(event, _store_error_text())
        return

    papi_match = re.fullmatch(r"(?i)papi\s*(.+)", msg)
    if papi_match:
        prompt = papi_match.group(1).strip()
        if _is_crypto_query(prompt):
            _reply_text(event, "Papi 分析目前不支援虛擬貨幣。")
        elif OPENALICE_API_URL and OPENALICE_API_TOKEN:
            try:
                _reply_text(event, call_openalice(prompt))
            except (requests.RequestException, ValueError, TypeError):
                _reply_text(event, "Papi 分析服務暫時無法回應，請稍後再試。")
        else:
            try:
                reply = call_papi_gemini_fallback(prompt)
                _reply_text(event, reply or "Papi 分析服務尚未設定。")
            except Exception as exc:
                print(f"Papi Gemini fallback failed: {exc}")
                _reply_text(event, "Papi AI 摘要暫時失敗；你仍可直接輸入股票代號查看完整量化分析。")
        return

    calc_text = re.fullmatch(r"試算\s+([A-Za-z0-9]+)\s+([0-9]+(?:\.[0-9]+)?)", msg)
    if calc_text:
        code, name = search_stock_code(calc_text.group(1))
        if not code:
            _reply_text(event, "找不到這檔股票，請重新查詢後再操作。")
            return
        data = analyze(code)
        if not data:
            _reply_text(event, "查無資料，請稍後再試。")
            return
        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(
                alt_text=f"{name} 投資試算",
                contents=build_projection_flex(code, name, data, calc_text.group(2), web_root),
            ),
        )
        return
    if msg.startswith("試算"):
        _reply_text(event, "請用：試算 2330 100000，或先查詢股票後點選「投資試算」。")
        return

    if msg in ("大盤預測", "大盤", "今日盤勢"):
        data = analyze("TAIEX")
        if not data:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="大盤資料暫時無法取得，請稍後再試。"))
            return
        url = f"{web_root}/market"
        flex_content = build_stock_flex_message("TAIEX", "台股大盤 (加權指數)", data, url)
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="📊 台股大盤預測出爐，點擊查看！", contents=flex_content))
        
    elif msg in ("預測", "熱門產業"):
        qr, _ = build_category_quick_reply(1)
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="請選擇產業板塊", contents=build_welcome_flex(), quick_reply=qr))

    elif msg == "我的關注":
        if line_store is None:
            _reply_text(event, _store_error_text())
        elif not user_id:
            _reply_text(event, "無法識別 LINE 使用者，請從一對一聊天室操作。")
        elif state_load_failed:
            _reply_text(event, _store_error_text())
        else:
            line_bot_api.reply_message(
                event.reply_token,
                FlexSendMessage(
                    alt_text="我的關注",
                    contents=build_watchlist_flex(current_state, web_root),
                ),
            )

    elif msg == "強勢訊號":
        if line_store is None:
            _reply_text(event, _store_error_text())
        elif not user_id:
            _reply_text(event, "無法識別 LINE 使用者，請從一對一聊天室操作。")
        elif state_load_failed:
            _reply_text(event, _store_error_text())
        else:
            line_bot_api.reply_message(
                event.reply_token,
                FlexSendMessage(
                    alt_text="強勢訊號",
                    contents=build_strong_signals_flex(current_state, web_root),
                ),
            )

    elif msg == "提醒管理":
        if line_store is None:
            _reply_text(event, _store_error_text())
        elif not user_id:
            _reply_text(event, "無法識別 LINE 使用者，請從一對一聊天室操作。")
        elif state_load_failed:
            _reply_text(event, _store_error_text())
        else:
            line_bot_api.reply_message(
                event.reply_token,
                FlexSendMessage(
                    alt_text="提醒管理",
                    contents=build_alerts_flex(current_state),
                ),
            )

    elif msg == "完整分析":
        card = build_line_summary_card("量化分析總覽", ["從市場摘要、強勢訊號與產業雷達開始判讀。"], "開啟完整分析", f"{web_root}/dashboard")
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="開啟完整分析", contents=card))

    elif msg == "投資試算":
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="投資試算", contents=build_calculator_help_flex()))

    elif msg == "功能選單":
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="Stock Papi 功能選單", contents=build_line_navigation_flex(web_root)))
        
    elif msg.startswith("分類第_") and msg.endswith("頁"):
        try: p = int(msg.replace("分類第_", "").replace("頁", ""))
        except: p = 1
        qr, _ = build_category_quick_reply(p)
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="請選擇產業板塊", contents=build_welcome_flex(), quick_reply=qr))
        
    elif msg == "產業列表":
        lines = ["📚 產業分類總表\n"] + [f"{i}. {c}" for i, c in enumerate(industry_map.keys(), 1)]
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="\n".join(lines[:120])))
        
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
        
    elif msg == "免責聲明":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="本系統資訊僅供研究參考，不構成投資建議，投資盈虧請自負。"))

    elif msg == "新手教學":
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="🎓 新手快速上手指南", contents=build_tutorial_flex()))
        
    else:
        code, name = search_stock_code(msg)
        if code:
            data = analyze(code)
            if not data:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="查無資料，請稍後再試。"))
                return
            url = f"{request.host_url}stock/{code}".replace("http://", "https://")
            watched = bool(
                current_state
                and any(item.get("code") == code for item in current_state.get("watchlist", []))
            )
            flex_content = build_stock_flex_message(code, name, data, url, watched=watched)
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"📊 {name} ({code}) 預測出爐，點擊查看！", contents=flex_content))
        elif getattr(getattr(event, "source", None), "type", "user") == "user":
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入股票代碼，或輸入：今日盤勢 / 我的關注 / 提醒管理 / 完整分析"))

if __name__ == "__main__":
    app.run(host=LOCAL_HOST, port=int(os.environ.get("PORT", 5000)))
