# app.py
# v5.8 穩定版：新增首頁健康檢查端點防休眠，並梳理重複路由確保 Flask 正常啟動
# --------------------------------------------------

import os
import time
import datetime
import requests
import pandas as pd
import twstock
import xml.etree.ElementTree as ET
import urllib.parse
import numpy as np
import json
import google.generativeai as genai

from sklearn.preprocessing import StandardScaler
from lightgbm import LGBMClassifier
from flask import Flask, request, abort, render_template_string
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
BROADCAST_TOKEN = os.getenv("BROADCAST_TOKEN", "default_secret")

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash')
else:
    gemini_model = None

finmind_token = ""
CATEGORY_PAGE_SIZE = 12

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

def _clean_df(df):
    df[['Open', 'High', 'Low', 'Close']] = df[['Open', 'High', 'Low', 'Close']].replace(0, np.nan)
    df = df.dropna(subset=['Date', 'Close'])
    return df.sort_values('Date').drop_duplicates(subset=['Date'], keep='last').set_index("Date")

def get_data(code, days=730):
    start_date = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    end_date = datetime.datetime.now().strftime("%Y-%m-%d")
    
    finmind_login()
    try:
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {"dataset": "TaiwanStockPrice", "data_id": code, "start_date": start_date, "end_date": end_date}
        if finmind_token: params["token"] = finmind_token
        r = requests.get(url, params=params, timeout=8).json()
        if r.get("data"):
            df = pd.DataFrame(r["data"])
            df["Date"] = pd.to_datetime(df["date"], errors="coerce")
            df["Open"] = pd.to_numeric(df["open"], errors="coerce")
            df["High"] = pd.to_numeric(df["max"], errors="coerce")
            df["Low"] = pd.to_numeric(df["min"], errors="coerce")
            df["Close"] = pd.to_numeric(df["close"], errors="coerce")
            return _clean_df(df[["Date", "Open", "High", "Low", "Close"]])
    except: pass
    
    try:
        import yfinance as yf
        ticker = "^TWII" if code == "TAIEX" else f"{code}.TW"
        hist = yf.download(ticker, start=start_date, progress=False)
        if isinstance(hist.columns, pd.MultiIndex): hist.columns = hist.columns.droplevel(1)
        if not hist.empty and "Close" in hist.columns:
            df = hist.copy()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df.index.name = 'Date'
            df = df.reset_index()
            return _clean_df(df[["Date", "Open", "High", "Low", "Close"]])
    except: pass
    return pd.DataFrame()

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
    c = df["Close"]
    df['MA_5'], df['MA20'], df['RET_1'] = c.rolling(5).mean(), c.rolling(20).mean(), c.pct_change()
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

def run_ai_engine(df):
    try:
        feats = ['MA_5', 'MA20', 'RET_1', 'RSI', 'Volat', 'MACD_OSC', 'K', 'D']
        w_df = df.copy()
        w_df['T'] = (w_df['Close'].shift(-5) > w_df['Close']).astype(int)
        v_df = w_df.dropna(subset=['T']).copy()
        
        if len(v_df) < 60: return None
        split = int(len(v_df) * 0.8)
        
        sc = StandardScaler()
        X_tr = sc.fit_transform(v_df.iloc[:split][feats])
        model = LGBMClassifier(n_estimators=80, learning_rate=0.05, max_depth=4, random_state=42, verbose=-1)
        model.fit(X_tr, v_df.iloc[:split]['T'])
        
        df['AI_P'] = model.predict_proba(sc.transform(df[feats].ffill().bfill()))[:, 1] * 100
        
        X_te = sc.transform(v_df.iloc[split:][feats])
        probs = model.predict_proba(X_te)[:, 1]
        
        rets = (v_df.iloc[split:]['Close'].shift(-1) / v_df.iloc[split:]['Close'] - 1).fillna(0).values
        strat_ret = np.where(probs > 0.6, rets, 0)
        
        imps = model.feature_importances_
        f_map = {'MA_5':'5日均線動能', 'MA20':'月線趨勢支撐', 'RET_1':'反轉動能', 'RSI':'RSI 強弱度', 'Volat':'波動收斂度', 'MACD_OSC':'MACD 柱狀體動能', 'K':'KD K值', 'D':'KD D值'}
        top = [f"{f_map.get(f, f)} (貢獻度: {(i/sum(imps))*100:.1f}%)" for f, i in sorted(zip(feats, imps), key=lambda x:x[1], reverse=True)[:3]]
        while len(top) < 3: top.append("無")
        
        cum_ret = np.cumprod(1 + strat_ret)
        bh_ret = np.cumprod(1 + rets)
        
        strat_cum = cum_ret[-1] - 1 if len(cum_ret) > 0 else 0
        bh_cum = bh_ret[-1] - 1 if len(bh_ret) > 0 else 0
        win_rate = (strat_ret[strat_ret!=0]>0).mean()*100 if len(strat_ret[strat_ret!=0])>0 else 0
        trades = len(strat_ret[strat_ret!=0])
        
        days_in_test = len(rets)
        mdd = (cum_ret/np.maximum.accumulate(cum_ret)-1).min()*100 if len(cum_ret) > 0 else 0
        sharpe = (strat_ret.mean()/strat_ret.std())*np.sqrt(252) if strat_ret.std()!=0 else 0

        if trades == 0: conclusion = "⏸️ 訊號空窗：模型未發現高勝率進場點，選擇空手觀望。"
        elif strat_cum > bh_cum: conclusion = "✅ 策略優勢：高報酬且風險控制優異。" if sharpe > 1 else "✅ 擊敗大盤：能創造超額報酬。"
        else: conclusion = "🛡️ 下檔保護：大跌時具備避險作用。" if mdd > -15 else "⚠️ 模型失真：容易追高殺低。"

        return {
            "days": days_in_test, "strat_cum": strat_cum * 100, "bh_cum": bh_cum * 100,
            "win_rate": win_rate, "trades": trades, "mdd": mdd, "sharpe": sharpe, 
            "conclusion": conclusion, "top_features": top
        }
    except Exception as e: 
        print(f"回測引擎錯誤: {e}")
        return None

def get_ai_insight_for_broadcast(name, data, bt, news):
    if not gemini_model: return "未設定 API Key，無法生成觀點。"
    n_txt = "\n".join([n['title'] for n in news])
    prompt = f"""請以資深分析師語氣，針對{name}撰寫100字內洞見。不要廢話，直接給建議。
最新價:{data['price']}
AI勝率:{data['prob']}%
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
    if s_score > 70: prob += 2
    elif s_score < 30: prob -= 2
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
        "prob_h": json.dumps(tv_df[['Date','AI_P']].rename(columns={'Date':'time','AI_P':'value'}).to_dict('records')),
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

def market_forecast(): return analyze("TAIEX")

# ==================================================
# 5. UI 渲染
# ==================================================
def render_web(d):
    bt = d['bt']
    news_html = "".join([f'<a href="{n["link"]}" target="_blank" class="news-link">🔹 {n["title"]}</a>' for n in d['news']]) if d['news'] else "暫無相關新聞"
    
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
    🎯 真實 AI 預測勝率：<span class="highlight">{d['prob']}%</span>
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
        🎯 綜合 AI 勝率：<span class="highlight">{d['prob']}%</span>
    </div>
</div>

<div class="card small">
    <h2>📊 歷史回測報告 (近 {bt['days']} 交易日)</h2>
    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; margin-bottom: 20px;">
        <div style="background: rgba(0,0,0,0.25); padding: 15px; border-radius: 12px; text-align: center;"><div style="font-size: 13px; color: #aaa; margin-bottom: 5px;">AI 策略報酬</div><div class="highlight" style="font-size: 1.3em;">{bt['strat_cum']:.2f}%</div></div>
        <div style="background: rgba(0,0,0,0.25); padding: 15px; border-radius: 12px; text-align: center;"><div style="font-size: 13px; color: #aaa; margin-bottom: 5px;">買進持有報酬</div><div style="font-size: 1.3em; color: #ddd;">{bt['bh_cum']:.2f}%</div></div>
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
    return render_template_string(html)

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
                        { "type": "text", "text": "🎯 AI 勝率", "color": "#0f172a", "size": "md", "weight": "bold", "flex": 4 },
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

@app.route("/")
def home():
    """健康檢查端點：給外部監控服務敲擊，防止 Render 休眠"""
    return "AI Stock Bot is awake and running!", 200

@app.route("/stock/<code>")
def stock_page(code):
    d = analyze(code)
    return render_web(d) if d else "查無資料"

@app.route("/market")
def market_page():
    d = analyze("TAIEX")
    return render_web(d) if d else "資料更新中"

@app.route("/callback", methods=["POST"])
def callback():
    try: handler.handle(request.get_data(as_text=True), request.headers.get("X-Line-Signature", ""))
    except: abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    
    if msg == "大盤預測" or msg == "大盤":
        data = analyze("TAIEX")
        if not data:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="大盤資料暫時無法取得，請稍後再試。"))
            return
        url = f"{request.host_url}market".replace("http://", "https://")
        flex_content = build_stock_flex_message("TAIEX", "台股大盤 (加權指數)", data, url)
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="📊 台股大盤預測出爐，點擊查看！", contents=flex_content))
        
    elif msg == "預測":
        qr, txt = build_category_quick_reply(1)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=txt, quick_reply=qr))
        
    elif msg.startswith("分類第_") and msg.endswith("頁"):
        try: p = int(msg.replace("分類第_", "").replace("頁", ""))
        except: p = 1
        qr, txt = build_category_quick_reply(p)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=txt, quick_reply=qr))
        
    elif msg == "產業列表":
        lines = ["📚 產業分類總表\n"] + [f"{i}. {c}" for i, c in enumerate(industry_map.keys(), 1)]
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="\n".join(lines[:120])))
        
    elif msg.startswith("選產業_"):
        cat = msg.replace("選產業_", "")
        arr = industry_map.get(cat, [])[:10]
        if not arr: text = "❌ 無資料"
        else:
            lines = [f"📈 {cat} Top10\n", "🔥 激進型"] + [f"{i}. {c} {get_stock_name(c)}" for i, c in enumerate(arr[:5], 1)]
            lines += ["\n🛡 保守型"] + [f"{i}. {c} {get_stock_name(c)}" for i, c in enumerate(arr[5:10], 1)]
            text = "\n".join(lines)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))
        
    elif msg == "免責聲明":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="本系統資訊僅供研究參考，不構成投資建議，投資盈虧請自負。"))
        
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
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入股票代碼，或輸入：預測 / 大盤預測 / 產業列表"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
