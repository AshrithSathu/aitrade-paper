const $=id=>document.getElementById(id);
const money=v=>v==null?'Unavailable':Number(v).toLocaleString('en-US',{style:'currency',currency:'USD',maximumFractionDigits:4});
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const form=$('settings');let loaded=false,inFlight=false;
$('assets').innerHTML='<fieldset class="wide" style="grid-column:1/-1;border:0;padding:0"><legend>Trade these assets</legend>'+['BTC','ETH','SOL','XRP','DOGE','HYPE','BNB'].map(a=>`<label style="display:inline-flex;align-items:center;gap:6px;margin:8px"><input style="width:auto;margin:0" type="checkbox" name="assets" value="${a}">${a}</label>`).join('')+'</fieldset>';
const marketPanel=document.createElement('div');marketPanel.className='panel';marketPanel.innerHTML='<h2>All selected markets & underlying data</h2><div class="scroll"><table><thead><tr><th>Asset / phase</th><th>Up / Down asks</th><th>Live underlying</th><th>Recorded opening TWAP</th><th>Delta</th><th>Feed / health</th></tr></thead><tbody id="markets"></tbody></table></div>';
$('position').closest('.panel').before(marketPanel);
const reviewPanel=document.createElement('div');reviewPanel.className='panel';reviewPanel.innerHTML='<div class="row"><h2>Codex review</h2><button type="button" id="review">Review now</button></div><p id="reviewstatus" role="status">No review yet.</p><details><summary>Exact Codex input and decisions</summary><pre id="payload" style="white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;max-height:450px;overflow:auto"></pre></details><p class="sub">Each completed review is saved under data/polymarket/codex-reviews. Manual reviews are previews only. Scheduled reviews may trade only while trading is enabled.</p>';
$('activity').closest('.panel').before(reviewPanel);
reviewPanel.insertAdjacentHTML('beforeend','<details><summary>Codex account login</summary><p>Sign in separately on this server. Trading stays paused.</p><button type="button" id="codexlogin">Sign in to Codex</button><pre id="loginoutput" style="white-space:pre-wrap" aria-live="polite"></pre></details>');
$('codexlogin').onclick=()=>action('codex/login');
$('trades').closest('.panel').insertAdjacentHTML('beforeend','<a href="/api/history" target="_blank" rel="noopener">View complete event history (JSON)</a>');
async function action(path,body={}){
  try{const r=await fetch('/api/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),d=await r.json();if(!r.ok)throw Error(d.error);$('saved').textContent=path==='settings'?'Settings saved.':'Action completed.';await refresh()}
  catch(e){$('saved').textContent=e.message;$('notice').textContent=e.message}
}
$('start').onclick=()=>action('start');$('pause').onclick=()=>action('pause');$('review').onclick=()=>action('review');
form.onsubmit=e=>{e.preventDefault();const data=new FormData(form),body=Object.fromEntries(data);body.assets=data.getAll('assets');action('settings',body)};
async function refresh(){
 if(inFlight)return;inFlight=true;
 try{
  const loginResponse=await fetch('/api/codex/login');if(loginResponse.ok){const login=await loginResponse.json();$('loginoutput').textContent=login.output;$('codexlogin').disabled=login.running;}
  const r=await fetch('/api/status');if(!r.ok)throw Error('Status unavailable');const d=await r.json(),s=d.state,a=d.account,events=s.events;
  if(!loaded){Object.entries(d.settings).forEach(([k,v])=>{if(k==='assets')form.querySelectorAll('[name=assets]').forEach(el=>el.checked=v.includes(el.value));else form.elements[k].value=v});loaded=true}
  $('badge').textContent=s.halted?'Limit reached':d.paused?'Trading paused':'Trading';$('start').disabled=!d.paused||!!s.halted;$('pause').disabled=d.paused;
  $('notice').textContent=d.error?'Worker paused: '+d.error:d.settings.assets.some(asset=>d.markets[asset]?.underlying?.delta==null)?'Chainlink is not ready. See feed health below; entries require fresh TWAP data and a recorded opening tick.':d.paused?'Trading paused. Chainlink data is available; no AI orders execute. Marks and official settlements still update.':'Paper trading with Chainlink 60s TWAP. Settlement uses official Polymarket results.';
  ['cash','equity'].forEach(k=>$(k).textContent=money(a[k]));$('pnl').textContent=money(a.realized_pnl);$('pnl').className=Number(a.realized_pnl)<0?'bad':'good';$('unrealized').textContent=money(a.unrealized_pnl);
  const m=d.markets[d.settings.assets[0]];
  if(m){$('ticker').textContent=m.ticker;$('up').textContent=money(m.yes_ask_dollars);$('down').textContent=money(m.no_ask_dollars);$('upbid').textContent='Bid '+money(m.yes_bid_dollars);$('downbid').textContent='Bid '+money(m.no_bid_dollars);$('target').textContent='Recorded opening TWAP '+money(m.floor_strike);$('countdown').textContent=Math.max(0,(Date.parse(m.close_time)-Date.now())/60000).toFixed(1)+' minutes remaining'}
  $('updated').textContent=d.updated?'Updated '+new Date(d.updated).toLocaleTimeString():'Awaiting feed';
  $('markets').innerHTML=d.settings.assets.map(asset=>{const m=d.markets[asset],u=m?.underlying||{},age=m?(Date.now()-Date.parse(m.received_at))/1000:null,error=d.errors[asset]||u.error;return `<tr><td>${esc(asset)}<br>${esc(s.phases[asset]||'WAIT_DATA')}</td><td>${money(m?.yes_ask_dollars)} / ${money(m?.no_ask_dollars)}</td><td>${money(u.price)}</td><td>${money(u.open15m)}</td><td>${money(u.delta)}</td><td>${esc(error||u.source||'Waiting')}<br>${age==null?'No quote':age.toFixed(1)+'s since quote'}<br>${u.source_at?'Chainlink '+esc(new Date(u.source_at).toLocaleTimeString()):'No Chainlink timestamp'}<br>${esc(u.history?.error?'History: '+u.history.error:u.history?'TWAP history: '+u.history.samples+' observations in '+u.history.bars.length+' minute bars':'History pending')}</td></tr>`}).join('');
  const positions=[...Object.values(s.positions).map(p=>({...p,status:'Open'})),...Object.values(s.pending).map(p=>({...p,status:'Settlement pending'}))];
  $('position').innerHTML=positions.length?positions.map(p=>`<div class="event"><strong>${esc(p.asset)} ${esc(p.side)} · ${esc(p.status)}</strong><br>${esc(p.ticker)}<br>${esc(p.size)} contracts · Entry ${money(p.entry)} · Mark ${money(p.last_mark)}<br><span class="sub">Mark time ${esc(p.mark_at||'unknown')}</span></div>`).join(''):'No open or pending positions.';
  $('performance').textContent=`Live PnL ${money(a.live_pnl)} · ${a.trades} closed · ${a.wins} wins / ${a.losses} losses · Average profit ${money(a.average_profit)} · Average loss ${money(a.average_loss)}`;
  const used=Math.max(0,-Number(a.realized_pnl)),limit=Math.abs(Number(d.settings.daily_loss));$('lossbudget').textContent=limit?money(used)+' / '+money(limit):'Disabled';$('lossbar').style.width=(limit?Math.min(100,used/limit*100):0)+'%';
  const exits=events.filter(e=>e.kind==='exit');$('trades').innerHTML=exits.length?exits.slice().reverse().map(e=>`<tr><td>${esc(new Date(e.at).toLocaleString())}</td><td>${esc(e.asset)} ${esc(e.side)}</td><td>${esc(e.size)}</td><td>${money(e.entry)}</td><td>${money(e.exit)}</td><td class="${Number(e.pnl)<0?'bad':'good'}">${money(e.pnl)}</td><td>${esc(e.reason)}</td></tr>`).join(''):'<tr><td colspan="7">No closed trades yet.</td></tr>';
  $('activity').innerHTML=events.length?events.slice(-30).reverse().map(e=>`<div class="event"><span class="sub">${esc(new Date(e.at).toLocaleTimeString())}</span> <strong>${esc(e.decision||e.kind)}</strong> ${esc(e.asset||'')} ${esc(e.side||'')}<br>${esc(e.reason||e.ticker||'')}</div>`).join(''):'No activity yet.';
  $('reviewstatus').textContent=d.codex?d.codex.status+' · '+(d.codex.response?.reason||'Reviewing the complete snapshot…'):'No review yet. Review now sends the current complete snapshot.';
  $('payload').textContent=d.codex?JSON.stringify({input:d.codex.payload,output:d.codex.response},null,2):'No payload sent yet.';
  $('review').disabled=d.busy||d.codex?.status==='running';
 }catch(e){$('badge').textContent='Disconnected';$('notice').textContent='Dashboard disconnected. Check that python3 dashboard.py is running.'}
 finally{inFlight=false}
}
refresh();setInterval(refresh,1500);
