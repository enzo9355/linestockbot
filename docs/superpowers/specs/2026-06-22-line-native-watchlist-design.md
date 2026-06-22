# LINE 原生關注清單與提醒設計

## 目標

將關注股票、提醒設定與強勢訊號留在 LINE 對話內；Web 只提供完整圖表、模型解釋、新聞、風險與分析建議。使用者不需要額外註冊或登入，LINE `userId` 就是帳號識別。

## 範圍

- LINE 內加入、移除及查看關注股票。
- LINE 內建立、查看及刪除提醒。
- Firestore 持久化每位 LINE 使用者的資料，支援跨裝置與 Cloud Run 重啟。
- Cloud Scheduler 每個平日收盤後檢查提醒並預先計算個人化強勢訊號；若資料日期沒有更新則不推播。
- 條件符合時使用 LINE Push API 主動通知。
- Web 移除關注與提醒操作；舊 `/watchlist` 連結重新導向 Dashboard。

## 非目標

- 不建立另一套帳號、密碼或登入頁。
- 不建立 LIFF 小程式。
- 不宣稱即時報價。現有資料管線使用日線資料，因此價格提醒代表「最新可用收盤價達到門檻」。
- 不掃描全市場。強勢訊號只比較該使用者的關注股票，避免資料源配額與排程時間失控。
- 不新增 Firestore Python SDK；使用既有 `requests` 呼叫 Firestore REST API，避免增加冷啟動與記憶體負擔。

## LINE 互動

### 個股查詢卡

使用者輸入 `2330` 後，Flex Message 顯示摘要並提供：

1. `加入關注` 或 `移除關注`：Postback action，直接更新 Firestore。
2. `設定提醒`：Postback action，顯示提醒類型 Quick Reply。
3. `查看完整分析`：唯一 Web URI，前往 `/stock/2330`。

### 我的關注

輸入 `我的關注` 後，LINE 回覆最多 12 檔股票。每檔顯示代碼、名稱、最新收盤價、五日上漲機率及趨勢，並提供設定提醒、移除與完整分析動作。沒有資料時回覆加入方式，不導向 Web。

### 設定提醒

支援三種條件：

- 最新收盤價大於等於指定數字。
- 五日上漲機率大於等於指定百分比，合法範圍為 1–99。
- 趨勢轉為多頭或空頭。

數值型提醒使用兩步驟對話：使用者選擇類型後，系統在 Firestore 保存 10 分鐘的 pending state，下一則文字視為門檻值。使用者可輸入 `取消` 結束；逾時或格式錯誤時不建立提醒。每位使用者最多 20 條提醒。

### 強勢訊號

輸入 `強勢訊號` 後，LINE 讀取排程預先計算的個人快照，依五日上漲機率排序並顯示最多 5 檔。快照需標示資料日期；尚未產生時說明會在下一次收盤排程後提供，而不是在 Webhook 內同步分析多檔股票。

## 資料模型

Firestore collection：`line_users`。

每個文件 ID 使用 LINE `userId`；文件只保存一個 JSON 字串欄位 `state`，內容如下：

```json
{
  "watchlist": [{"code": "2330", "name": "台積電", "added_at": "2026-06-22T08:00:00Z"}],
  "alerts": [{"id": "uuid", "code": "2330", "kind": "price", "value": 1000, "enabled": true, "last_triggered_date": null}],
  "pending": {"code": "2330", "kind": "price", "expires_at": "2026-06-22T08:10:00Z"},
  "signals": {"as_of": "2026-06-22", "items": []}
}
```

整份文件讀寫是刻意的簡化：目前每位使用者資料量小，能避免額外資料層與 SDK。寫入使用 Firestore `updateTime` precondition；衝突時重新讀取、合併並重試一次，避免 Webhook 與排程同時更新造成資料遺失。若未來單一使用者寫入量明顯增加，再拆成子集合。

## Firestore 存取

- Cloud Run 從 metadata server 取得服務帳號 access token並短暫快取。
- 使用 Firestore REST `documents:get`、`documents:patch` 與 `documents:list`。
- `state` 使用 `json.dumps`／`json.loads`，寫入前套用固定 schema、數量上限與欄位驗證。
- GET 保存文件 `updateTime`；PATCH 帶入相同 precondition，衝突時只重試一次。
- 文件不存在時視為空狀態；暫時性錯誤回覆「目前無法更新，請稍後再試」，不假裝成功。
- Cloud Run 服務帳號只授予 Firestore 需要的最小讀寫權限。

必要環境變數：

- `GCP_PROJECT_ID`
- `ALERT_TASK_TOKEN`

## 排程與通知

Cloud Scheduler 在平日收盤資料可用後呼叫 `POST /tasks/check-alerts`，並用 `Authorization: Bearer <ALERT_TASK_TOKEN>` 驗證。端點流程：

1. 讀取所有有關注或啟用提醒的使用者。
2. 將股票代碼去重，每檔只執行一次 `analyze()`。
3. 若最新資料日期未改變則結束；否則更新各使用者的 `signals` 快照。
4. 評估啟用提醒；同一提醒每天最多觸發一次。
5. 透過 LINE Push API 發送符合條件的卡片，再寫入 `last_triggered_date`。

若單次執行接近 Cloud Run timeout，端點回傳非 2xx，讓 Cloud Scheduler 依既有重試策略重跑。每日去重欄位防止重複推播。

## Web 調整

- 個股頁移除「加入關注」與「設定提醒」。
- Dashboard 移除本機關注與最近提醒區塊。
- 移除瀏覽器 `localStorage` 關注、提醒及觸發紀錄程式碼。
- `/watchlist` 保留相容性重新導向 `/dashboard`，避免舊 LINE 訊息成為死連結。
- LINE 導流文件更新為：關注、提醒與強勢訊號留在 LINE；只有完整分析開啟 Web。

## 安全與隱私

- 不將 LINE `userId` 輸出到一般應用程式日誌或 Web 頁面。
- Scheduler token 使用 `hmac.compare_digest` 驗證，不接受 query string token。
- Postback 中的股票代碼、提醒類型與數值全部重新驗證，不信任 LINE 用戶端資料。
- Firestore 資料不開放公用網路讀寫，只允許 Cloud Run 服務帳號。
- Push API 失敗時保留提醒為未觸發，讓下次排程重試。

## 測試

- Firestore state encode／decode、空文件與損壞資料回復。
- Firestore `updateTime` 衝突重新讀取、合併一次，第二次衝突明確失敗。
- 加入重複股票、12 檔上限、移除股票。
- pending state 成功、取消、過期及錯誤數值。
- 收盤價、五日機率與趨勢條件評估。
- 每日去重及 Push API 失敗不標記成功。
- Scheduler 未設定或 token 錯誤時拒絕執行。
- Web 不再出現關注／提醒按鈕，舊 `/watchlist` 正確重新導向。
- 完整回歸、Bandit、pip-audit、Python 編譯與正式 Cloud Run 路由驗證。

## 部署順序

1. 在 GCP 建立 Firestore database。
2. 授予 Cloud Run 服務帳號最小 Firestore 權限。
3. 設定 `GCP_PROJECT_ID` 與 `ALERT_TASK_TOKEN`。
4. 部署程式並驗證 LINE Postback 與 Firestore 寫入。
5. 建立 Cloud Scheduler 收盤後工作。
6. 以測試帳號驗證跨裝置關注、提醒觸發及每日去重。
