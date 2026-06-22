const bySelector=(selector,root=document)=>root.querySelector(selector);

function setActivePeriod(button){
  document.querySelectorAll('[data-period]').forEach(item=>item.classList.toggle('active',item===button));
  document.documentElement.dataset.period=button.dataset.period;
}

document.addEventListener('click',event=>{
  const period=event.target.closest('[data-period]');
  if(period)setActivePeriod(period);
});

async function loadDashboard(){
  const page=bySelector('[data-dashboard-endpoint]');
  if(!page)return;
  try{
    const response=await fetch(page.dataset.dashboardEndpoint,{headers:{Accept:'application/json'}});
    if(!response.ok)throw new Error('dashboard');
    const data=await response.json();
    renderDashboard(data);
  }catch(error){
    bySelector('[data-dashboard-error]').hidden=false;
  }
}

function renderDashboard(data){
  const market=bySelector('[data-market-summary]');
  market.innerHTML=`<article class="metric"><span>加權指數</span><strong>${data.market.price.toFixed(2)}</strong></article><article class="metric"><span>五日上漲機率</span><strong>${data.market.prob}%</strong></article><article class="metric"><span>趨勢</span><strong>${data.market.trend}</strong></article>`;
  bySelector('.status-dot').textContent='已更新';
  const opportunities=bySelector('[data-opportunities]');
  opportunities.innerHTML=data.opportunities.length?data.opportunities.map(item=>`<a class="signal-row" href="/stock/${item.code}"><span><b>${item.code}</b> ${item.name}</span><strong>${item.prob}%</strong></a>`).join(''):'<div class="empty-state">尚無近期分析紀錄；先從 LINE 或個股搜尋開始。</div>';
  bySelector('[data-sector-grid]').innerHTML=data.sectors.map(item=>`<div class="sector-chip"><strong>${item.name}</strong><div class="muted small">追蹤 ${item.count} 檔</div></div>`).join('');
}

loadDashboard();

function initStockChart(){
  const container=bySelector('#stock-chart');
  const source=bySelector('#stock-chart-data');
  if(!container||!source||!window.LightweightCharts)return;
  const raw=JSON.parse(source.textContent);
  const candles=JSON.parse(raw.candles);
  const chart=LightweightCharts.createChart(container,{width:container.clientWidth,height:430,layout:{background:{color:'transparent'},textColor:'#8fa4bd'},grid:{vertLines:{color:'#14263b'},horzLines:{color:'#14263b'}},timeScale:{borderColor:'#20344e'}});
  const candleSeries=chart.addCandlestickSeries({upColor:'#ff6b75',downColor:'#36d399',borderVisible:false,wickUpColor:'#ff6b75',wickDownColor:'#36d399'});
  candleSeries.setData(candles);
  chart.addLineSeries({color:'#39c6a3',lineWidth:1,title:'MA20'}).setData(JSON.parse(raw.ma20));
  chart.addLineSeries({color:'#f5bb4b',lineWidth:2,lineStyle:2,title:'五日預測'}).setData(JSON.parse(raw.prediction));
  window.stockChart={chart,length:candles.length};
  setChartRange(90);
  window.addEventListener('resize',()=>chart.resize(container.clientWidth,430));
}

function setChartRange(days){
  if(!window.stockChart)return;
  const {chart,length}=window.stockChart;
  chart.timeScale().setVisibleLogicalRange({from:Math.max(0,length-days),to:length+5});
}

document.addEventListener('click',event=>{
  const range=event.target.closest('[data-chart-range]');
  if(!range)return;
  document.querySelectorAll('[data-chart-range]').forEach(item=>item.classList.toggle('active',item===range));
  setChartRange(Number(range.dataset.chartRange));
});

initStockChart();
