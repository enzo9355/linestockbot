# Stock Papi Responsive UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the web product into the approved Stock Papi bright-theme experience with a responsive dashboard, a cleaner stock detail page, and a restrained liquid-glass interaction layer.

**Architecture:** Keep the existing Flask + Jinja + one CSS file + one JS file shape. Reuse the current `/dashboard`, `/api/dashboard`, `/stock/<code>`, and `/market` routes, add only the minimum extra dashboard payload needed for Industry Forecast and Top Picks, and keep watchlist management on LINE by making the web watchlist section informational rather than stateful.

**Tech Stack:** Python 3.10, Flask/Jinja, vanilla JavaScript, CSS custom properties, Lightweight Charts, unittest, Figma file `2WTxdIkhixC8CUToToCLTi`.

---

## File Map

- `app.py`
  - Keep existing routes.
  - Expand `/api/dashboard` payload with sector forecast cards and top-pick summaries.
- `templates/base.html`
  - Rebrand shell, fonts, title, desktop sidebar, and mobile navigation labels.
- `templates/dashboard.html`
  - Replace the old dark "market summary / strong signals / sector radar" shell with the new six-section landing page.
- `templates/stock_detail.html`
  - Reorder the page to conclusion -> chart -> data -> news -> guidance and add glass-only-on-controls treatment.
- `static/app.css`
  - Replace dark tokens with bright tokens, typography variables, responsive layout rules, and liquid-glass utility styles.
- `static/app.js`
  - Render the new dashboard payload and make chart height responsive without overflow.
- `tests/test_web_product.py`
  - Protect the new shell, dashboard sections, chart safety, and analysis-only web rules.

### Task 1: Create the Figma foundations and two screen families

**Files:**
- External: `https://www.figma.com/design/2WTxdIkhixC8CUToToCLTi`
- Reference: `templates/base.html`
- Reference: `templates/dashboard.html`
- Reference: `templates/stock_detail.html`
- Reference: `static/app.css`

- [ ] **Step 1: Inspect the current Figma file and current web shell**

Read the current Figma file metadata and take one screenshot of each existing page. Then compare that against the current web shell so the new frames follow the real product structure.

Expected result:

- The file contains or is updated to contain pages named `Foundations`, `Components`, `Desktop`, and `Mobile`.
- The current web references are the existing dashboard and stock detail pages rather than a generic finance mockup.

- [ ] **Step 2: Build the foundations page**

Create a `Foundations` page containing:

- Color swatches for `Warm Background`, `Mint`, `Apricot`, `Lavender`, `Line Soft`, `Text Strong`, `Text Muted`, `Taiwan Up`, `Taiwan Down`
- Type samples for:
  - `Display 32/40`
  - `H1 24/32`
  - `H2 20/28`
  - `H3 16/24`
  - `Body 15/24`
  - `Caption 12/18`
- A liquid-glass tile that shows:
  - translucent fill
  - highlight stroke
  - soft outer shadow
  - reduced-transparency fallback variant

Validation checklist:

- `Lora` is used on English / numeric samples.
- Chinese samples use the `GenWanMin TW` stack label, with a serif fallback note if the exact font is unavailable in Figma.
- The page is readable on a light background and does not use a dark finance palette.

- [ ] **Step 3: Build the components page**

Create a `Components` page with these components or local patterns:

- `Hero Search`
- `Market Pulse Card`
- `Watchlist Info Chip`
- `Industry Forecast Card`
- `Top Pick Card`
- `Glass Segmented Control`
- `Chart Toolbar`
- `Risk Note Card`
- `Mobile Floating Action Bar`

Validation checklist:

- Only the search, segmented control, floating bar, and summary capsule use the glass treatment.
- Cards that carry dense numbers stay solid.

- [ ] **Step 4: Build the desktop screens**

Create two desktop frames at `1440px` width:

- `Desktop / Dashboard`
  - Hero Search
  - Market Pulse
  - LINE-native watchlist explainer strip
  - Industry Forecast
  - Top Picks
  - Learn / Explain
- `Desktop / Stock Detail`
  - summary strip
  - chart card
  - calculator / foreign flow row
  - technical / explainability row
  - risk block
  - news block

Validation checklist:

- `Industry Forecast` is the dominant mid-page section.
- The chart frame has visible safe padding and does not visually bleed into adjacent cards.

- [ ] **Step 5: Build the mobile screens**

Create two mobile frames at `390px` width:

- `Mobile / Dashboard`
- `Mobile / Stock Detail`

Both must use a single-column flow. The dashboard order must be:

1. Search
2. Market Pulse
3. LINE watchlist guidance
4. Industry Forecast
5. Top Picks
6. Learn / Explain

Validation checklist:

- The mobile bottom action bar exists.
- Industry cards stay single-column.
- The chart block remains inside its card with no overflow.

### Task 2: Rebrand the global shell and theme tokens

**Files:**
- Modify: `templates/base.html`
- Modify: `static/app.css`
- Test: `tests/test_web_product.py`

- [ ] **Step 1: Write the failing shell/theme test**

Add a test that checks the new brand, bright-shell markers, and font split:

```python
def test_base_shell_uses_stock_papi_brand_and_light_theme(self):
    response = stock_app.app.test_client().get("/dashboard")
    html = response.get_data(as_text=True)
    css = Path(stock_app.app.static_folder, "app.css").read_text(encoding="utf-8")

    self.assertIn("Stock Papi", html)
    self.assertIn("市場首頁", html)
    self.assertIn("Lora", html)
    self.assertIn("GenWanMin", css)
    self.assertIn("--bg:#f6efe6", css)
    self.assertIn(".glass-panel", css)
    self.assertNotIn("量化觀測站", html)
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_web_product.WebProductTests.test_base_shell_uses_stock_papi_brand_and_light_theme -v
```

Expected: FAIL because the shell still renders `量化觀測站`, dark theme colors, and the old Inter / Noto Sans stack.

- [ ] **Step 3: Implement the minimum base-shell rewrite**

Update the base template to rebrand the app and switch the font includes:

```html
<title>{% block title %}Stock Papi{% endblock %}</title>
<link href="https://fonts.googleapis.com/css2?family=Lora:wght@500;600;700&family=Noto+Serif+TC:wght@400;500;600;700&display=swap" rel="stylesheet">
...
<a class="brand" href="{{ url_for('dashboard_page') }}">
  <span class="brand-mark">SP</span><span>Stock Papi</span>
</a>
<nav class="nav-list">
  <a class="nav-link {% block nav_dashboard %}{% endblock %}" href="{{ url_for('dashboard_page') }}">市場首頁</a>
  <a class="nav-link" href="{{ url_for('market_page') }}">大盤分析</a>
</nav>
```

Replace the root CSS tokens with a light palette and font variables:

```css
:root{
  --bg:#f6efe6;
  --surface:#fffaf4;
  --surface-2:#fff4ea;
  --surface-3:#f3eadf;
  --line:#dbcdbd;
  --text:#2a211b;
  --muted:#74685d;
  --mint:#7fd7c4;
  --apricot:#f4b58a;
  --lavender:#b8a6ea;
  --up:#d94b63;
  --down:#1f9a72;
  --font-display:"Lora", serif;
  --font-body:"GenWanMin TW","Noto Serif TC",serif;
  --radius:24px;
}
body{background:radial-gradient(circle at top,#fffdf8 0,#f6efe6 38%,#f0e7dc 100%);color:var(--text);font-family:var(--font-body)}
.glass-panel{background:rgba(255,255,255,.2);border:1px solid rgba(255,255,255,.7);box-shadow:0 18px 45px rgba(84,61,39,.12),inset 0 1px 0 rgba(255,255,255,.68);backdrop-filter:blur(18px)}
```

- [ ] **Step 4: Run the shell/theme tests and verify GREEN**

Run:

```powershell
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_web_product.WebProductTests.test_base_shell_uses_stock_papi_brand_and_light_theme tests.test_web_product.WebProductTests.test_web_shell_supports_keyboard_and_mobile_interactions -v
```

Expected: PASS. The old keyboard / reduced-motion coverage still passes and the new shell test passes.

- [ ] **Step 5: Commit the theme shell**

```powershell
git add templates/base.html static/app.css tests/test_web_product.py
git commit -m "feat: rebrand web shell as Stock Papi"
```

### Task 3: Replace the dashboard with the approved six-section landing page

**Files:**
- Modify: `app.py`
- Modify: `templates/dashboard.html`
- Modify: `static/app.js`
- Modify: `static/app.css`
- Test: `tests/test_web_product.py`

- [ ] **Step 1: Write the failing dashboard tests**

Extend the dashboard shell and API tests:

```python
def test_dashboard_page_is_the_stock_papi_landing_page(self):
    response = stock_app.app.test_client().get("/dashboard")
    html = response.get_data(as_text=True)

    for label in ["市場摘要", "產業預測", "精選標的", "新手投資小辭典", "LINE 管理關注"]:
        self.assertIn(label, html)
    self.assertNotIn("強勢訊號", html)
    self.assertIn('data-top-picks', html)
    self.assertIn('data-watchlist-strip', html)

@patch.object(stock_app, "analyze")
@patch.object(stock_app, "load_sector_signal_snapshot")
def test_dashboard_api_returns_sector_cards_and_top_picks(self, load_snapshot, analyze):
    analyze.return_value = {"price": 23150.0, "prob": 58, "trend": "多頭"}
    load_snapshot.return_value = {
        "sectors": {
            "半導體": [{"code": "2330", "name": "台積電", "prob": 72, "trend": "多頭", "score": 91.2, "as_of": "2026-06-28", "foreign_net_5": 12000}],
            "AI 伺服器": [{"code": "6669", "name": "緯穎", "prob": 69, "trend": "多頭", "score": 88.5, "as_of": "2026-06-28", "foreign_net_5": 5400}],
        }
    }

    response = stock_app.app.test_client().get("/api/dashboard")
    payload = response.get_json()

    self.assertEqual(payload["sector_cards"][0]["name"], "半導體")
    self.assertEqual(payload["sector_cards"][0]["leader"]["code"], "2330")
    self.assertEqual(len(payload["top_picks"]), 2)
```

- [ ] **Step 2: Run the dashboard tests and verify RED**

Run:

```powershell
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_web_product.WebProductTests.test_dashboard_page_is_the_stock_papi_landing_page tests.test_web_product.WebProductTests.test_dashboard_api_returns_sector_cards_and_top_picks -v
```

Expected: FAIL because the current template still renders `強勢訊號` / `產業雷達` and the API does not return `sector_cards` or `top_picks`.

- [ ] **Step 3: Add the minimum dashboard payload helpers in `app.py`**

Add small helpers that reuse existing cached data and the saved sector snapshot:

```python
def dashboard_sector_cards(limit=6):
    if not line_store:
        return []
    try:
        snapshot = load_sector_signal_snapshot(line_store)
    except Exception:
        return []
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
                "code": leader.get("code", ""),
                "name": leader.get("name", ""),
                "prob": int(_safe_float(leader.get("prob"))),
                "trend": leader.get("trend", "中性"),
                "foreign_net_5": int(_safe_float(leader.get("foreign_net_5"))),
                "as_of": leader.get("as_of", ""),
            },
        })
    return sorted(cards, key=lambda item: item["score"], reverse=True)[:limit]

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
```

Then update `/api/dashboard`:

```python
sector_cards = dashboard_sector_cards()
return jsonify({
    "market": {...},
    "opportunities": cached_opportunities(),
    "sector_cards": sector_cards,
    "top_picks": dashboard_top_picks(sector_cards),
    "watchlist_hint": {
        "title": "關注與提醒在 LINE 管理",
        "steps": ["在 LINE 查詢個股", "點選加入關注", "從提醒管理設定通知"],
    },
})
```

- [ ] **Step 4: Rebuild the dashboard template and renderer**

Replace `templates/dashboard.html` with the six-section skeleton:

```html
<div class="page dashboard-page" data-dashboard-endpoint="/api/dashboard">
  <section class="hero-search glass-panel">
    <p class="eyebrow">STOCK PAPI</p>
    <h1>今天先看市場，再決定要不要出手。</h1>
    <p class="muted">產業預測放中間，完整分析留到個股頁。</p>
    <div class="hero-shortcuts">
      <a href="/stock/2330">台積電</a><a href="/market">大盤</a><a href="#industry-forecast">產業預測</a>
    </div>
  </section>

  <section class="panel"><div class="section-heading"><div><p class="eyebrow">MARKET PULSE</p><h2>市場摘要</h2></div></div><div class="pulse-grid" data-market-summary></div></section>
  <section class="panel"><div class="section-heading"><div><p class="eyebrow">LINE FIRST</p><h2>LINE 管理關注</h2></div></div><div class="watchlist-strip" data-watchlist-strip></div></section>
  <section class="panel" id="industry-forecast"><div class="section-heading"><div><p class="eyebrow">INDUSTRY FORECAST</p><h2>產業預測</h2></div></div><div class="forecast-grid" data-sector-grid></div></section>
  <section class="panel"><div class="section-heading"><div><p class="eyebrow">TOP PICKS</p><h2>精選標的</h2></div></div><div class="top-picks" data-top-picks></div></section>
  <section class="panel"><div class="section-heading"><div><p class="eyebrow">LEARN</p><h2>新手投資小辭典</h2></div></div><div class="learn-grid"><article>AI 勝率是什麼？</article><article>市場情緒怎麼用？</article></div></section>
</div>
```

Update the dashboard renderer in `static/app.js`:

```javascript
function renderDashboard(data){
  bySelector('[data-market-summary]').innerHTML = `
    <article class="pulse-card"><span>加權指數</span><strong>${data.market.price.toFixed(2)}</strong></article>
    <article class="pulse-card"><span>五日上漲機率</span><strong>${data.market.prob}%</strong></article>
    <article class="pulse-card"><span>技術趨勢</span><strong>${data.market.trend}</strong></article>`;
  bySelector('[data-watchlist-strip]').innerHTML = data.watchlist_hint.steps.map(step=>`<article class="watch-chip">${step}</article>`).join('');
  bySelector('[data-sector-grid]').innerHTML = data.sector_cards.map(card=>`<a class="forecast-card" href="/stock/${card.leader.code}"><span>${card.name}</span><strong>${card.leader.name}</strong><small>AI 勝率 ${card.leader.prob}%・${card.leader.trend}</small></a>`).join('');
  bySelector('[data-top-picks]').innerHTML = data.top_picks.map(item=>`<a class="pick-card" href="/stock/${item.code}"><strong>${item.name}</strong><p>${item.summary}</p></a>`).join('');
}
```

- [ ] **Step 5: Run the dashboard tests and verify GREEN**

Run:

```powershell
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_web_product.WebProductTests.test_dashboard_page_is_the_stock_papi_landing_page tests.test_web_product.WebProductTests.test_dashboard_api_returns_sector_cards_and_top_picks tests.test_web_product.WebProductTests.test_web_is_analysis_only_and_old_watchlist_redirects -v
```

Expected: PASS. The dashboard shows the new section labels, the API emits `sector_cards` and `top_picks`, and the `/watchlist` redirect behavior still holds.

- [ ] **Step 6: Commit the dashboard rewrite**

```powershell
git add app.py templates/dashboard.html static/app.js static/app.css tests/test_web_product.py
git commit -m "feat: rebuild dashboard as Stock Papi landing page"
```

### Task 4: Reorder and restyle the stock detail page without chart overflow

**Files:**
- Modify: `templates/stock_detail.html`
- Modify: `static/app.css`
- Modify: `static/app.js`
- Test: `tests/test_web_product.py`

- [ ] **Step 1: Write the failing stock-page tests**

Add two tests:

```python
@patch.object(stock_app, "analyze", return_value=analysis_data())
def test_stock_page_uses_summary_chart_news_first_flow(self, _analyze):
    response = stock_app.app.test_client().get("/stock/2330")
    html = response.get_data(as_text=True)

    self.assertIn("預測摘要", html)
    self.assertIn("價格與預測軌跡", html)
    self.assertIn("近期新聞", html)
    self.assertIn("新手解讀", html)
    self.assertIn("glass-segmented", html)

def test_stock_chart_height_is_measured_from_container_width(self):
    js = Path(stock_app.app.static_folder, "app.js").read_text(encoding="utf-8")
    css = Path(stock_app.app.static_folder, "app.css").read_text(encoding="utf-8")

    self.assertIn("function measureChartHeight", js)
    self.assertIn("Math.min(460", js)
    self.assertIn(".chart-shell", css)
    self.assertIn("overflow:hidden", css)
```

- [ ] **Step 2: Run the stock-page tests and verify RED**

Run:

```powershell
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_web_product.WebProductTests.test_stock_page_uses_summary_chart_news_first_flow tests.test_web_product.WebProductTests.test_stock_chart_height_is_measured_from_container_width -v
```

Expected: FAIL because the current page has the old dark card layout and the chart height is still a fixed `430px`.

- [ ] **Step 3: Rewrite the stock detail markup with a chart-safe wrapper**

Restructure `templates/stock_detail.html` so the top half is:

```html
<header class="stock-hero">
  <div class="hero-copy">
    <a class="back-link" href="{{ url_for('dashboard_page') }}">返回市場首頁</a>
    <p class="eyebrow">{{ d.code }}</p>
    <h1>{{ d.name }}</h1>
    <p class="muted">先看結論，再往下看圖表與新聞。</p>
  </div>
  <section class="summary-strip glass-panel" aria-label="預測摘要">...</section>
</header>

<section class="panel chart-shell">
  <div class="section-heading">
    <div><p class="eyebrow">PRICE & FORECAST</p><h2>價格與預測軌跡</h2></div>
    <div class="segmented glass-segmented" aria-label="圖表顯示期間">...</div>
  </div>
  <div id="stock-chart" class="stock-chart" aria-label="{{ d.name }}互動式價格圖"></div>
</section>
```

Rename the later education block to `新手解讀`, keep calculator / foreign flow / technicals / explainability, and keep the page analysis-only by not reintroducing watchlist actions.

- [ ] **Step 4: Make chart sizing width-driven in `static/app.js` and add the new CSS wrappers**

Add a tiny helper:

```javascript
function measureChartHeight(container){
  return Math.max(320, Math.min(460, Math.round(container.clientWidth * 0.62)));
}
```

Use it in `initStockChart()`:

```javascript
const height = measureChartHeight(container);
const chart = LightweightCharts.createChart(container,{width:container.clientWidth,height,...});
...
const resize = ()=>chart.resize(container.clientWidth, measureChartHeight(container));
```

And add the chart-safe CSS:

```css
.chart-shell{overflow:hidden;padding:24px}
.stock-chart{width:100%;max-width:100%;min-height:320px;border-radius:20px}
.stock-hero{display:grid;grid-template-columns:1.05fr .95fr;gap:18px;margin-bottom:20px}
.summary-strip{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;padding:18px}
.glass-segmented{padding:4px;border-radius:999px}
@media(max-width:900px){
  .stock-hero,.analysis-grid{grid-template-columns:1fr}
  .summary-strip,.indicator-grid{grid-template-columns:1fr 1fr}
}
```

- [ ] **Step 5: Run the stock-page tests and verify GREEN**

Run:

```powershell
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_web_product.WebProductTests.test_stock_page_uses_summary_chart_news_first_flow tests.test_web_product.WebProductTests.test_stock_chart_height_is_measured_from_container_width tests.test_web_product.WebProductTests.test_stock_page_is_the_core_analysis_workspace -v
```

Expected: PASS. The stock page keeps the calculator / foreign flow sections, adds the new layout markers, and the chart now sizes from container width instead of a hardcoded height.

- [ ] **Step 6: Commit the stock detail rewrite**

```powershell
git add templates/stock_detail.html static/app.css static/app.js tests/test_web_product.py
git commit -m "feat: redesign stock detail page"
```

### Task 5: Final verification, README note, and second review

**Files:**
- Modify: `README.md`
- Test: `tests/test_web_product.py`
- Test: full suite

- [ ] **Step 1: Add one README note about the new web split**

Add a short section stating:

```markdown
## Web 與 LINE 分工

- LINE：加入關注、提醒管理、產業預測入口
- Web：完整圖表、新聞情緒、回測與白話解讀
```

- [ ] **Step 2: Run the focused web suite**

Run:

```powershell
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_web_product -v
```

Expected: PASS.

- [ ] **Step 3: Run the full test suite**

Run:

```powershell
& 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests -v
```

Expected: PASS. Existing deprecation warnings may remain, but no new failures should appear.

- [ ] **Step 4: Check the diff and run the required second review**

Run:

```powershell
git diff --check
git status --short
```

Then send only the diff plus the test summary to `agy`. Do not treat an empty `agy --print` response as approval; inspect the log output too.

- [ ] **Step 5: Commit the final integration**

```powershell
git add README.md templates/base.html templates/dashboard.html templates/stock_detail.html static/app.css static/app.js app.py tests/test_web_product.py docs/superpowers/plans/2026-06-28-stock-papi-responsive-ui.md
git commit -m "feat: ship Stock Papi responsive web UI"
```
