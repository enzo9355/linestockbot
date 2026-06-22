# LINE → Web 導流規格

LINE 僅負責快速摘要、提醒與導流；完整圖表、模型解釋及條件管理都在 Web 完成。

## Rich Menu 四個入口

| 區塊 | LINE 動作 | Web 目的地 |
| --- | --- | --- |
| 今日盤勢 | 傳送文字 `今日盤勢`，回覆大盤摘要卡 | `/market` |
| 熱門產業 | 傳送文字 `熱門產業`，開啟產業 Quick Reply | `/dashboard#sectors` |
| 我的關注 | 傳送文字 `我的關注`，回覆單一 CTA 卡 | `/watchlist` |
| 完整分析 | 傳送文字 `完整分析`，回覆單一 CTA 卡 | `/dashboard` |

LINE Official Account Manager 建立 Rich Menu 時，依上表設定四個 message action。`功能選單` 可用來預覽相同資訊架構，不需要額外後端狀態。

## Flex Message 結構

所有卡片共用 `build_line_summary_card()`，維持一致的深色表面、綠色主動作，且每張卡只保留一個明確 CTA。

| 卡片 | 摘要內容 | CTA |
| --- | --- | --- |
| 每日摘要 | 大盤趨勢、五日上漲機率、風險提示 | `/market` |
| 強勢股票 | 股票名稱、最新價格、五日上漲機率 | `/stock/<code>` |
| 異常波動 | 漲跌或量能異常及白話說明 | `/stock/<code>` |
| 關注提醒 | 觸發條件、目前值、觸發時間 | `/watchlist#alerts` |

目前專案沒有 LIFF ID、LINE 使用者身分綁定或持久化資料庫，因此先使用一般 HTTPS Web 路由。未來加入 LIFF 時，只需將 Rich Menu 與 Flex URI 換成對應 LIFF URL，Web 頁面與資料流程不必重寫。
