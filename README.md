# AI Quant Investment LINE Bot

一個面向股票新手的台股量化分析 LINE Bot。它把技術指標、模型機率、新聞情緒、法人籌碼與回測結果整理成容易理解的 LINE 卡片與 Web 分析頁，讓使用者可以先在 LINE 快速操作，再到 Web 查看完整圖表與細節。

> 本專案提供的資訊僅供研究與學習參考，不構成投資建議；投資決策與盈虧需由使用者自行承擔。

## 主要功能

- LINE 股票查詢
  - 輸入股票代碼或名稱，例如 `2330`
  - 回覆最新收盤價、五日上漲機率、趨勢、新聞情緒與操作按鈕

- 六格 Rich Menu
  - 今日盤勢
  - 我的關注
  - 強勢訊號
  - 提醒管理
  - 投資試算
  - 完整分析

- 關注清單與提醒
  - 在 LINE 內加入 / 移除關注股票
  - 支援股價門檻、機率門檻、趨勢提醒
  - 使用 Firestore 保存每位 LINE 使用者狀態

- 強勢訊號
  - 針對使用者自己的關注清單排序
  - 依五日上漲機率挑出目前較強的標的

- 投資試算
  - 股票卡內可點「投資試算」
  - 預設金額：1 萬、5 萬、10 萬
  - 也支援自訂文字格式：`試算 2330 100000`
  - 估算約可買股數、AI 策略歷史損益、買進持有歷史損益

- Web 完整分析頁
  - `/dashboard`：市場摘要、強勢訊號、產業雷達
  - `/stock/<code>`：互動式 K 線圖、五日預測軌跡、技術指標、模型解釋、投資試算、外資買賣超、新聞與風險提示
  - `/market`：台股大盤分析

## 技術架構

```text
LINE 使用者
  │
  ▼
LINE Messaging API
  │
  ▼
Flask / Gunicorn on Cloud Run
  ├─ 股票資料：FinMind、twstock、yfinance
  ├─ 新聞資料：Google News RSS
  ├─ 量化模型：LightGBM + scikit-learn
  ├─ 狀態儲存：Firestore REST API
  ├─ 定時提醒：Cloud Scheduler → /tasks/check-alerts
  └─ Web UI：Jinja templates + Vanilla JS + Lightweight Charts
```

設計原則：

- LINE 負責快速互動：關注、提醒、強勢訊號、投資試算入口。
- Web 負責完整分析：圖表、回測、模型解釋、籌碼與風險提示。
- 避免增加重型 NLP / ML 套件，降低 Cloud Run 冷啟動與記憶體壓力。
- Webhook 路徑保持輕量，避免 LINE 5 秒 timeout。

## 核心檔案

| 檔案 | 說明 |
| --- | --- |
| `app.py` | Flask app、LINE webhook、股票分析、Flex Message、Web routes |
| `line_state.py` | LINE 使用者狀態、Firestore REST 存取、關注與提醒規則 |
| `templates/` | Dashboard 與個股分析頁 |
| `static/app.js` | Dashboard 載入、K 線圖、投資金額即時計算 |
| `static/app.css` | Web UI 樣式 |
| `tests/` | unittest 測試 |
| `Dockerfile` | Cloud Run 部署映像 |
| `docs/line-to-web-map.md` | LINE 與 Web 分工規格 |

## 環境變數

必要：

| 變數 | 說明 |
| --- | --- |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Messaging API access token |
| `LINE_CHANNEL_SECRET` | LINE webhook signature secret |
| `GEMINI_API_KEY` | Gemini API key，用於生成摘要觀點 |

選用：

| 變數 | 說明 |
| --- | --- |
| `FINMIND_USER` | FinMind 帳號，用於取得 token |
| `FINMIND_PASSWORD` | FinMind 密碼 |
| `GCP_PROJECT_ID` | 啟用 Firestore 關注與提醒功能 |
| `ALERT_TASK_TOKEN` | Cloud Scheduler 呼叫 `/tasks/check-alerts` 的 Bearer token |
| `BROADCAST_TOKEN` | 週報廣播端點驗證 token |
| `HOST` | 本機開發綁定 host，預設 `127.0.0.1` |
| `PORT` | 服務 port，Cloud Run 會自動注入 |

不要把任何 token、密碼或金鑰提交到 Git。

## 本機開發

建議使用 Python 3.10。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:LINE_CHANNEL_ACCESS_TOKEN="test"
$env:LINE_CHANNEL_SECRET="test"
python app.py
```

預設啟動於：

```text
http://127.0.0.1:5000
```

## 測試

```powershell
$env:LINE_CHANNEL_ACCESS_TOKEN="test"
$env:LINE_CHANNEL_SECRET="test"
python -m unittest discover -s tests -v
python -m py_compile app.py line_state.py
node --check static/app.js
git diff --check
```

如果使用外部依賴安裝目錄，例如 `.deps`：

```powershell
$env:PYTHONPATH="C:\Users\enzo\Documents\line bot\.deps"
python -m unittest discover -s tests -v
```

## 部署

目前專案以 Dockerfile 部署到 Google Cloud Run。

```powershell
gcloud run deploy line-stock-bot `
  --source . `
  --region asia-east1 `
  --project line-stock-bot-498908 `
  --allow-unauthenticated
```

正式服務會由 Cloud Run 注入 `$PORT`，Dockerfile 透過 Gunicorn 綁定：

```text
gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 app:app
```

## LINE 使用方式

常用文字：

| 輸入 | 行為 |
| --- | --- |
| `2330` | 查詢台積電 |
| `今日盤勢` | 查看台股大盤 |
| `我的關注` | 查看關注清單 |
| `強勢訊號` | 查看關注股票中的強勢標的 |
| `提醒管理` | 查看與取消提醒 |
| `投資試算` | 查看試算操作說明 |
| `試算 2330 100000` | 以 100000 元試算 2330 |
| `完整分析` | 開啟 Web 儀表板 |
| `功能選單` | 預覽六格功能選單 |

## 資料與模型說明

- 預測目標：未來五個交易日方向。
- 模型：LightGBM binary classifier。
- 驗證：時間序列切分，避免用未來資料訓練過去。
- 回測：使用五日報酬，並扣除估計交易成本。
- 輔助特徵：
  - 均線、RSI、MACD、KD、波動率
  - 成交量變化
  - 法人 / 外資買賣超
  - 融資與融券變化

外資買賣超目前作為輔助判讀與模型特徵之一，不會單獨構成買賣建議。

## 安全與維運注意事項

- `/callback` 會驗證 LINE signature。
- `/tasks/check-alerts` 僅接受 `Authorization: Bearer <ALERT_TASK_TOKEN>`。
- Firestore 資料只保存 LINE userId 對應的關注、提醒與快照，不保存使用者個人投資紀錄。
- Rich Menu 可透過 LINE Messaging API 更新；若只改 LINE Official Account Manager 後台圖片，repo 不會自動同步。
- 若 Cloud Run source deploy 發生暫存 bucket 權限問題，需確認 Cloud Run / Compute service account 是否具備必要的 Storage object 讀取權限。

## 目前限制

- 預測與回測不代表未來績效。
- 新聞情緒是輔助資訊，不會直接覆蓋模型機率。
- FinMind 或 Yahoo 資料缺漏時，部分籌碼或價格資訊可能暫時不可用。
- Cloud Run scale-to-zero 會有冷啟動；新增功能時應避免模組載入階段做重運算或網路請求。
