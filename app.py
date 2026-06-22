# app.py
# v5.8 穩定版：新增首頁健康檢查端點防休眠，並梳理重複路由確保 Flask 正常啟動
# --------------------------------------------------

import os
import time
import datetime
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
from flask import Flask, request, abort, render_template, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage,
    QuickReply, QuickReplyButton, MessageAction
)

# ==================================================
# 1. 基本設定與系統快取
# ==================================================
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
FINMIND_USER = os.getenv("FINMIND_USER")
FINMIND_PASSWORD = os.getenv("FINMIND_PASSWORD")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LOCAL_HOST = os.getenv("HOST", "127.0.0.1")
BROADCAST_TOKEN = os.getenv("BROADCAST_TOKEN", "default_secret")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash')
else:
    gemini_model = None

finmind_token = ""
CATEGORY_PAGE_SIZE = 12
PREDICTION_HORIZON = 5
ROUND_TRIP_COST = 0.00585
ENTRY_THRESHOLD = 0.60
MODEL_FEATURES = [
    "MA_5", "MA20", "RET_1", "RET_5", "RET_20", "RSI", "Volat",
    "RANGE_PCT", "VOL_RATIO", "VOL_CHG", "INST_NET_RATIO", "MARGIN_CHG",
    "SHORT_CHG", "MACD_OSC", "K", "D",
]

_SYSTEM_CACHE = {}
CACHE_EXPIRY_SECONDS = 3600  

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
    except: pass

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

def merge_chip_data(price, institutional=None, margin=None):
    result = price.copy()
    if institutional is not None and not institutional.empty:
        flows = institutional.copy()
        flows["Date"] = pd.to_datetime(flows["date"], errors="coerce")
        flows["buy"] = pd.to_numeric(flows["buy"], errors="coerce").fillna(0)
        flows["sell"] = pd.to_numeric(flows["sell"], errors="coerce").fillna(0)
        flows["InstitutionalNet"] = flows["buy"] - flows["sell"]
        flows = flows.groupby("Date", as_index=False)["InstitutionalNet"].sum()
        result = result.merge(flows, on="Date", how="left")
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
    for column in ["InstitutionalNet", "MarginBalance", "ShortBalance"]:
        if column not in result:
            result[column] = 0.0
        result[column] = result[column].fillna(0.0)
    return result

def _clean_df(df):
    df[['Open', 'High', 'Low', 'Close']] = df[['Open', 'High', 'Low', 'Close']].replace(0, np.nan)
    for column in ["Volume", "InstitutionalNet", "MarginBalance", "ShortBalance"]:
        if column not in df:
            df[column] = 0.0
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0.0)
    df = df.dropna(subset=['Date', 'Close'])
    return df.sort_values('Date').drop_duplicates(subset=['Date'], keep='last').set_index("Date")

def get_data(code, days=730):
    start_date = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    end_date = datetime.datetime.now().strftime("%Y-%m-%d")
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
        try:
            import yfinance as yf
            tickers = ["^TWII"] if code == "TAIEX" else [f"{code}.TW", f"{code}.TWO"]
            for ticker in tickers:
                hist = yf.download(ticker, start=start_date, progress=False)
                if isinstance(hist.columns, pd.MultiIndex):
                    hist.columns = hist.columns.droplevel(1)
                if not hist.empty and "Close" in hist.columns:
                    price = hist.copy()
                    price.index = pd.to_datetime(price.index).tz_localize(None)
                    price.index.name = "Date"
                    price = price.reset_index()[
                        ["Date", "Open", "High", "Low", "Close", "Volume"]
                    ]
                    break
        except Exception as exc:
            print(f"Yahoo Finance 讀取失敗: {exc}")
    if price is None:
        return pd.DataFrame()

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
def get_news(name):
    try:
        q = urllib.parse.quote(f"{name} 股票")
        url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        r = requests.get(url, timeout=5)
        root = ET.fromstring(r.text)
        return [{"title": i.find('title').text, "link": i.find('link').text} for i in root.findall('.//item')[:5]]
    except: return []

def calc_all(df):
    df = df.copy()
    for column in ["Volume", "InstitutionalNet", "MarginBalance", "ShortBalance"]:
        if column not in df:
            df[column] = 0.0
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
def analyze_sentiment(news_list):
    if not news_list: return 50, "中性"
    scores = []
    pos_words = ["漲", "紅", "高", "多", "買", "利多", "創紀錄", "看好", "強", "優", "雙位數", "營收增", "獲利", "新高", "上揚", "突破"]
    neg_words = ["跌", "綠", "低", "空", "賣", "利空", "虧", "看壞", "弱", "劣", "崩", "違約", "衰退", "下修", "降評", "保守", "跳水"]
    for n in news_list:
        t = n['title']
        s = 0.5
        # 基於自訂關鍵字的輕量級情緒分析
        for w in pos_words: 
            if w in t: s += 0.15
        for w in neg_words: 
            if w in t: s -= 0.15
        scores.append(max(0, min(1, s)))
    avg_s = sum(scores) / len(scores) * 100
    if avg_s >= 65: return avg_s, "🔥 樂觀貪婪"
    elif avg_s <= 35: return avg_s, "😨 悲觀恐慌"
    else: return avg_s, "⚖️ 中性觀望"

def _do_analyze(code):
    df = get_data(code)
    if df.empty or len(df) < 200: return None
    df = calc_all(df)
    bt = run_ai_engine(df)
    if not bt: return None
    
    last = df.iloc[-1]
    name = get_stock_name(code)
    news = get_news(name)
    
    s_score, s_status = analyze_sentiment(news)
    prob = last['AI_P']
    prob = int(max(0, min(100, prob)))
    
    trend = "多頭" if last['Close'] > last['MA20'] else "空頭"
    
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

    return {
        "code": code, "name": name, "price": last['Close'], "prob": prob, 
        "bt": bt, "news": news, "trend": trend,
        "rsi": last['RSI'], "ma20": last['MA20'],
        "macd_osc": last['MACD_OSC'], "k": last['K'], "d": last['D'],
        "s_score": s_score, "s_status": s_status,
        "candles": json.dumps(tv_df[['Date','Open','High_corr','Low_corr','Close']].rename(columns={'Date':'time','Open':'open','High_corr':'high','Low_corr':'low','Close':'close'}).to_dict('records')),
        "ma20_line": json.dumps(tv_df[['Date','MA20']].dropna().rename(columns={'Date':'time','MA20':'value'}).to_dict('records')),
        "prob_h": json.dumps(tv_df[['Date','AI_P']].dropna().rename(columns={'Date':'time','AI_P':'value'}).to_dict('records')),
        "pred": json.dumps(pred)
    }

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

def market_forecast(): return analyze("TAIEX")

# ==================================================
# 5. UI 渲染
# ==================================================
def render_web(d):
    bt = d['bt']
    news_html = "".join(
        f'<a href="{escape(str(n["link"]), quote=True)}" target="_blank" rel="noopener noreferrer" class="news-link">🔹 {escape(str(n["title"]))}</a>'
        for n in d['news']
    ) if d['news'] else "暫無相關新聞"
    
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
        <span style="font-size: 24px; font-weight: bold; color: {'#ef5350' if d['s_score']<40 else '#26a69a'};">{d['s_score']:.1f} ({d['s_status']})</span>
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
def build_market_map():
    market = {"全市場": [], "ETF專區": [], "AI伺服器": []}
    ai_names = {"鴻海", "廣達", "緯創", "英業達", "仁寶", "和碩", "華碩", "微星", "技嘉", "神達", "緯穎", "勤誠", "雙鴻", "奇鋐", "宏碁"}
    for code, info in twstock.codes.items():
        if len(code) not in [4, 5]: continue
        grp = getattr(info, "group", None) or getattr(info, "type", None)
        if grp and isinstance(grp, str) and grp.strip():
            grp = grp.strip()
            if grp not in market: market[grp] = []
            market[grp].append(code)
            market["全市場"].append(code)
            if code.startswith("00"): market["ETF專區"].append(code)
            if info.name in ai_names: market["AI伺服器"].append(code)
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
    if request.args.get("token") != BROADCAST_TOKEN: return "身份驗證失敗", 403
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
def build_stock_flex_message(code, name, data, url):
    color_prob = "#10b981" if data['prob'] >= 50 else "#ef4444"
    color_s = "#10b981" if data['s_score'] >= 50 else "#ef4444"
    color_trend = "#10b981" if "多" in data['trend'] else "#ef4444"

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
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#0284c7",
                    "action": {
                        "type": "uri",
                        "label": "📈 查看圖表與回測報告",
                        "uri": url
                    }
                }
            ]
        }
    }

def build_line_summary_card(title, lines, cta_label, url, accent="#39c6a3"):
    """建立只有一個 Web CTA 的 LINE 摘要卡。"""
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
                "action": {"type": "uri", "label": cta_label, "uri": url},
            }],
        },
    }

def build_line_navigation_flex(base_url):
    """Rich Menu 四個入口的可預覽 Flex 版本。"""
    root = base_url.rstrip("/")
    entries = [
        ("今日盤勢", "先看大盤趨勢與五日上漲機率", "查看盤勢", f"{root}/market"),
        ("熱門產業", "快速比較近期訊號較強的產業", "查看產業", f"{root}/dashboard#sectors"),
        ("我的關注", "追蹤自選股票與條件提醒", "開啟關注", f"{root}/watchlist"),
        ("完整分析", "進入量化儀表板做完整判讀", "開啟分析", f"{root}/dashboard"),
    ]
    return {
        "type": "carousel",
        "contents": [build_line_summary_card(title, [description], cta, url) for title, description, cta, url in entries],
    }

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

@app.route("/")
def home():
    """健康檢查端點：給外部監控服務敲擊，防止 Render 休眠"""
    return "AI Stock Bot is awake and running!", 200

@app.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html")

@app.route("/watchlist")
def watchlist_page():
    return render_template("watchlist.html")

@app.route("/api/dashboard")
def dashboard_api():
    market = analyze("TAIEX")
    if not market:
        return jsonify({"error": "market data unavailable"}), 503
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
        "sectors": sectors,
    })

@app.route("/api/stock/<code>/summary")
def stock_summary_api(code):
    if code not in twstock.codes:
        abort(404)
    data = analyze(code)
    if not data:
        return jsonify({"error": "stock data unavailable"}), 503
    return jsonify({
        "code": code,
        "name": data["name"],
        "price": float(data["price"]),
        "prob": int(data["prob"]),
        "trend": data["trend"],
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

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    web_root = request.host_url.replace("http://", "https://").rstrip("/")

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
        card = build_line_summary_card("我的關注", ["查看自選股票、提醒規則與最近觸發紀錄。"], "開啟關注清單", f"{web_root}/watchlist")
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="開啟我的關注", contents=card))

    elif msg == "完整分析":
        card = build_line_summary_card("量化分析總覽", ["從市場摘要、強勢訊號與產業雷達開始判讀。"], "開啟完整分析", f"{web_root}/dashboard")
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="開啟完整分析", contents=card))

    elif msg == "功能選單":
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="量化觀測站功能選單", contents=build_line_navigation_flex(web_root)))
        
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
        arr = industry_map.get(cat, [])[:10]
        if not arr:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 無資料"))
        else:
            flex_content = build_industry_carousel(cat, arr)
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"📈 {cat} 推薦名單出爐！", contents=flex_content))
        
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
            flex_content = build_stock_flex_message(code, name, data, url)
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"📊 {name} ({code}) 預測出爐，點擊查看！", contents=flex_content))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入股票代碼，或輸入：今日盤勢 / 熱門產業 / 我的關注 / 完整分析"))

if __name__ == "__main__":
    app.run(host=LOCAL_HOST, port=int(os.environ.get("PORT", 5000)))
