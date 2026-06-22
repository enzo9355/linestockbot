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
