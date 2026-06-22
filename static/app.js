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

const STORE={watchlist:'quant-watchlist-v1',alerts:'quant-alerts-v1',log:'quant-alert-log-v1'};
const readStore=(key)=>{try{return JSON.parse(localStorage.getItem(key))||[]}catch{return []}};
const writeStore=(key,value)=>localStorage.setItem(key,JSON.stringify(value));
const escapeHTML=(value)=>String(value).replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));

function showToast(message){
  const toast=bySelector('[data-toast]');
  if(!toast)return;
  toast.textContent=message;toast.hidden=false;
  setTimeout(()=>toast.hidden=true,2200);
}

function currentStock(){
  const page=bySelector('[data-stock-code]');
  return page?{code:page.dataset.stockCode,name:page.dataset.stockName}:null;
}

function addWatchlist(stock=currentStock()){
  if(!stock)return;
  const items=readStore(STORE.watchlist);
  if(items.some(item=>item.code===stock.code)){showToast('已在關注清單中');return;}
  if(items.length>=12){showToast('關注清單最多 12 檔');return;}
  writeStore(STORE.watchlist,[...items,stock]);
  showToast(`已加入 ${stock.name}`);renderWatchlist();renderLocalPreviews();
}

function removeWatchlist(code){
  writeStore(STORE.watchlist,readStore(STORE.watchlist).filter(item=>item.code!==code));
  renderWatchlist();renderLocalPreviews();
}

function prepareAlertDialog(){
  const dialog=bySelector('[data-alert-dialog]');
  const select=bySelector('[data-alert-stock]');
  if(!dialog||!select)return;
  const stocks=readStore(STORE.watchlist);
  const active=currentStock();
  const merged=active&&!stocks.some(item=>item.code===active.code)?[active,...stocks]:stocks;
  select.innerHTML=merged.map(item=>`<option value="${escapeHTML(item.code)}">${escapeHTML(item.code)} ${escapeHTML(item.name)}</option>`).join('');
  if(!merged.length){showToast('請先加入一檔關注股票');return;}
  dialog.showModal();
}

function saveAlert(form){
  const data=new FormData(form);const value=String(data.get('value')).trim();
  if(!value){showToast('請輸入條件值');return;}
  const stock=readStore(STORE.watchlist).find(item=>item.code===data.get('code'))||currentStock();
  if(!stock)return;
  const alerts=readStore(STORE.alerts);
  alerts.push({id:Date.now(),code:stock.code,name:stock.name,kind:data.get('kind'),value});
  writeStore(STORE.alerts,alerts);form.closest('dialog').close();form.reset();
  showToast('提醒已儲存');renderAlerts();
}

function renderWatchlist(){
  const root=bySelector('[data-watchlist-list]');if(!root)return;
  const items=readStore(STORE.watchlist);bySelector('[data-empty-state]').hidden=items.length>0;
  root.innerHTML=items.map(item=>`<article class="watch-row" data-watch-code="${escapeHTML(item.code)}"><a href="/stock/${escapeHTML(item.code)}"><b>${escapeHTML(item.code)}</b><span>${escapeHTML(item.name)}</span></a><span class="watch-metrics">載入中</span><button class="icon-button" data-watch-remove="${escapeHTML(item.code)}" aria-label="移除">×</button></article>`).join('');
  loadWatchlistQuotes(items);
}

async function loadWatchlistQuotes(items){
  for(const item of items){
    try{
      const response=await fetch(`/api/stock/${item.code}/summary`);if(!response.ok)throw new Error('quote');
      const quote=await response.json();const row=bySelector(`[data-watch-code="${item.code}"] .watch-metrics`);
      if(row)row.innerHTML=`<strong>${quote.price.toFixed(2)}</strong><span class="${quote.prob>=50?'positive':'negative'}">${quote.prob}%</span>`;
      evaluateAlerts(quote);
    }catch{const row=bySelector(`[data-watch-code="${item.code}"] .watch-metrics`);if(row)row.textContent='暫時無資料';}
  }
}

function evaluateAlerts(quote){
  const today=new Date().toISOString().slice(0,10);const log=readStore(STORE.log);
  for(const alert of readStore(STORE.alerts).filter(item=>item.code===quote.code)){
    const target=Number(alert.value);const hit=alert.kind==='price'?quote.price>=target:alert.kind==='probability'?quote.prob>=target:quote.trend===alert.value;
    if(hit&&!log.some(item=>item.alertId===alert.id&&item.date===today))log.unshift({alertId:alert.id,date:today,code:quote.code,name:quote.name,message:`${alert.kind} 達到 ${alert.value}`});
  }
  writeStore(STORE.log,log.slice(0,20));renderAlertLog();
}

function renderAlerts(){
  const root=bySelector('[data-alert-list]');if(!root)return;
  const labels={price:'價格門檻',probability:'機率門檻',technical:'技術條件'};
  const alerts=readStore(STORE.alerts);
  root.innerHTML=alerts.length?alerts.map(item=>`<article class="rule-row"><span><b>${escapeHTML(item.code)}</b> ${labels[item.kind]}：${escapeHTML(item.value)}</span><button class="icon-button" data-alert-remove="${item.id}" aria-label="刪除">×</button></article>`).join(''):'<div class="empty-state">尚未設定提醒</div>';
}

function renderAlertLog(){
  const root=bySelector('[data-alert-log]');if(!root)return;
  const log=readStore(STORE.log);root.innerHTML=log.length?log.map(item=>`<article class="log-row"><b>${escapeHTML(item.code)} ${escapeHTML(item.name)}</b><span>${escapeHTML(item.message)}</span><small>${item.date}</small></article>`).join(''):'<div class="empty-state">目前沒有觸發紀錄</div>';
}

function renderLocalPreviews(){
  const watch=bySelector('[data-watchlist-preview]');if(watch){const items=readStore(STORE.watchlist);watch.innerHTML=items.length?items.slice(0,4).map(item=>`<a class="preview-link" href="/stock/${escapeHTML(item.code)}">${escapeHTML(item.code)} ${escapeHTML(item.name)}</a>`).join(''):'尚未加入關注股票';}
  const alert=bySelector('[data-alert-preview]');if(alert){const items=readStore(STORE.log);alert.innerHTML=items.length?items.slice(0,3).map(item=>`<div class="preview-link">${escapeHTML(item.code)} ${escapeHTML(item.message)}</div>`).join(''):'目前沒有觸發提醒';}
}

document.addEventListener('click',event=>{
  if(event.target.closest('[data-watchlist-add]'))addWatchlist();
  if(event.target.closest('[data-alert-open]'))prepareAlertDialog();
  const remove=event.target.closest('[data-watch-remove]');if(remove)removeWatchlist(remove.dataset.watchRemove);
  const removeAlert=event.target.closest('[data-alert-remove]');if(removeAlert){writeStore(STORE.alerts,readStore(STORE.alerts).filter(item=>item.id!==Number(removeAlert.dataset.alertRemove)));renderAlerts();}
});
bySelector('[data-alert-form]')?.addEventListener('submit',event=>{event.preventDefault();saveAlert(event.currentTarget)});
renderWatchlist();renderAlerts();renderAlertLog();renderLocalPreviews();
