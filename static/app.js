const bySelector = (selector, root = document) => root.querySelector(selector);

async function loadDashboard() {
  const page = bySelector("[data-dashboard-endpoint]");
  if (!page) return;
  try {
    const response = await fetch(page.dataset.dashboardEndpoint, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("dashboard");
    renderDashboard(await response.json());
  } catch (error) {
    const banner = bySelector("[data-dashboard-error]");
    if (banner) banner.hidden = false;
  }
}

function renderDashboard(data) {
  const market = bySelector("[data-market-summary]");
  if (market) {
    market.innerHTML = `
      <article class="pulse-card"><span>加權指數</span><strong>${data.market.price.toFixed(2)}</strong><small class="muted">先看盤勢，再決定要不要出手</small></article>
      <article class="pulse-card"><span>五日上漲機率</span><strong>${data.market.prob}%</strong><small class="muted">用機率取代情緒化追價</small></article>
      <article class="pulse-card"><span>技術趨勢</span><strong>${data.market.trend}</strong><small class="muted">搭配產業預測一起看</small></article>`;
  }
  const status = bySelector(".status-dot");
  if (status) status.textContent = "已更新";

  const watchlist = bySelector("[data-watchlist-strip]");
  if (watchlist) {
    const hint = data.watchlist_hint || { title: "", steps: [] };
    watchlist.innerHTML = (hint.steps || []).map((step, index) =>
      `<article class="watch-chip"><span>Step ${index + 1}</span><strong>${step}</strong></article>`
    ).join("");
  }

  const forecasts = bySelector("[data-sector-grid]");
  if (forecasts) {
    const cards = data.sector_cards || [];
    forecasts.innerHTML = cards.length ? cards.map((card) => `
      <a class="forecast-card" href="${card.leader.code ? `/stock/${card.leader.code}` : '/dashboard'}">
        <span>${card.name}</span>
        <strong>${card.leader.name || "等待更新"}</strong>
        <small>AI 勝率 ${card.leader.prob}%・${card.leader.trend}</small>
        <small>外資5日 ${Number(card.leader.foreign_net_5 || 0).toLocaleString("zh-TW")}・${card.leader.as_of || "快照待更新"}</small>
      </a>`).join("") : '<div class="empty-state">產業預測快照尚未準備好，請稍後再試。</div>';
  }

  const picks = bySelector("[data-top-picks]");
  if (picks) {
    const items = data.top_picks || [];
    picks.innerHTML = items.length ? items.map((item) => `
      <a class="pick-card" href="/stock/${item.code}">
        <strong>${item.name}</strong>
        <p>${item.headline}</p>
        <p>${item.summary}</p>
      </a>`).join("") : '<div class="empty-state">目前沒有足夠的精選標的資料。</div>';
  }
}

function formatNumber(value) {
  return Number.isFinite(value) ? Math.round(value).toLocaleString("zh-TW") : "—";
}

function initReturnCalculator() {
  const panel = bySelector("[data-return-calculator]");
  if (!panel) return;
  const input = bySelector("[data-investment-amount]", panel);
  const price = Number(panel.dataset.price);
  const strategyReturn = Number(panel.dataset.strategyReturn);
  const buyholdReturn = Number(panel.dataset.buyholdReturn);
  const update = () => {
    const amount = Number(input.value);
    const shares = Math.floor(amount / price);
    const deployed = shares * price;
    const valid = Number.isFinite(amount) && amount > 0 && price > 0 && shares > 0;
    bySelector("[data-shares]", panel).textContent = valid ? shares.toLocaleString("zh-TW") : "—";
    bySelector("[data-deployed]", panel).textContent = valid ? formatNumber(deployed) : "—";
    bySelector("[data-strategy-profit]", panel).textContent = valid ? formatNumber((deployed * strategyReturn) / 100) : "—";
    bySelector("[data-buyhold-profit]", panel).textContent = valid ? formatNumber((deployed * buyholdReturn) / 100) : "—";
  };
  input.addEventListener("input", update);
  update();
}

function measureChartHeight(container) {
  return Math.max(320, Math.min(460, Math.round(container.clientWidth * 0.62)));
}

function setChartRange(days) {
  if (!window.stockChart) return;
  const { chart, length } = window.stockChart;
  chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, length - days), to: length + 5 });
}

function initStockChart() {
  const container = bySelector("#stock-chart");
  const source = bySelector("#stock-chart-data");
  if (!container || !source || !window.LightweightCharts) return;
  const raw = JSON.parse(source.textContent);
  const candles = JSON.parse(raw.candles);
  const height = measureChartHeight(container);
  const chart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height,
    layout: { background: { color: "transparent" }, textColor: "#74685d" },
    grid: { vertLines: { color: "#e7dacd" }, horzLines: { color: "#e7dacd" } },
    timeScale: { borderColor: "#dbcdbd" },
  });
  const candleSeries = chart.addCandlestickSeries({
    upColor: "#d94b63",
    downColor: "#1f9a72",
    borderVisible: false,
    wickUpColor: "#d94b63",
    wickDownColor: "#1f9a72",
  });
  candleSeries.setData(candles);
  chart.addLineSeries({ color: "#7fd7c4", lineWidth: 1, title: "MA20" }).setData(JSON.parse(raw.ma20));
  chart.addLineSeries({ color: "#c98542", lineWidth: 2, lineStyle: 2, title: "五日預測" }).setData(JSON.parse(raw.prediction));
  window.stockChart = { chart, length: candles.length };
  setChartRange(90);
  const resize = () => chart.resize(container.clientWidth, measureChartHeight(container));
  if (window.ResizeObserver) new ResizeObserver(resize).observe(container);
  window.addEventListener("resize", resize);
}

document.addEventListener("click", (event) => {
  const range = event.target.closest("[data-chart-range]");
  if (!range) return;
  document.querySelectorAll("[data-chart-range]").forEach((item) => {
    item.classList.toggle("active", item === range);
    item.setAttribute("aria-pressed", item === range);
  });
  setChartRange(Number(range.dataset.chartRange));
});

loadDashboard();
initStockChart();
initReturnCalculator();
