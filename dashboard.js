const $=id=>document.getElementById(id);
const money=v=>v==null?'—':Number(v).toLocaleString('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2});
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const time=v=>new Date(v).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
const form=$('settings');let loaded=false,settings=null,selectedMinutes='15',actionBusy=false;const loadedModes=new Set();let latest=null;
function tabsDisabled(value){$('tab5').disabled=value;$('tab15').disabled=value}
for(const minutes of ['5','15'])$('tab'+minutes).onclick=()=>{if(actionBusy)return;selectedMinutes=minutes;loaded=false;settings=null;$('saved').textContent='';for(const m of ['5','15']){$('tab'+m).setAttribute('aria-selected',String(m===minutes));$('tab'+m).className=m===minutes?'':'secondary'}if(latest)render(latest)};
async function action(path,body={},minutes=selectedMinutes){
 if(actionBusy)return;actionBusy=true;tabsDisabled(true);
 try{const r=await fetch('/api/'+path+'?minutes='+minutes,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),d=await r.json();if(!r.ok)throw Error(d.error);$('saved').textContent=path==='settings'?'Limits saved.':'Done.'}
 catch(e){$('saved').textContent=e.message}
 finally{actionBusy=false;tabsDisabled(false)}
}
for(const m of ['5','15']){$('start'+m).onclick=()=>{if($('runhours'+m).reportValidity()&&$('profittarget'+m).reportValidity())action('start',{duration_hours:$('runhours'+m).value,profit_target_percent:$('profittarget'+m).value},m)};$('pause'+m).onclick=()=>action('pause',{},m);}
$('codexlogin').onclick=()=>action('codex/login');
form.onsubmit=e=>{e.preventDefault();if(!settings)return;const body={...settings,...Object.fromEntries(new FormData(form)),assets:['BTC']};body.daily_loss=String(-Math.abs(Number(body.daily_loss)));action('settings',body)};
function render(update){
  latest=update;
  const d=update.accounts[selectedMinutes],s=d.state,a=d.account,m=d.markets.BTC,u=m?.underlying;
  settings=d.settings;
  $('markettitle').textContent='Bitcoin · '+selectedMinutes+' minutes';
  $('reviewpolicy').textContent='One review at minute '+(selectedMinutes==='5'?'1':'3')+'. Enter Up, Down, or skip. Trades stay open until settlement.';
  for(const [mode,info] of Object.entries(d.modes)){
   $('mode'+mode).textContent=info.halted?'Limit reached':info.paused?'Stopped':'Running';
   $('start'+mode).disabled=!info.paused||!!info.halted;$('pause'+mode).disabled=info.paused;
   $('runhours'+mode).disabled=!info.paused;$('profittarget'+mode).disabled=!info.paused;
   if(!loadedModes.has(mode)){$('runhours'+mode).value=info.run_hours||12;$('profittarget'+mode).value=info.profit_target_percent||0;loadedModes.add(mode)}
  }
  if(!loaded){for(const k of ['balance','max_trade','size','daily_loss'])form.elements[k].value=k==='daily_loss'?Math.abs(Number(settings[k])):settings[k];loaded=true}
  $('badge').textContent=s.halted?'Limit reached':d.paused?'Paused':'Running';
  $('notice').textContent=d.error?'Trading paused: '+d.error:d.paused?(s.stop_reason?s.stop_reason+'. AI is stopped.':'Paused. AI is stopped.'):'Paper trading is running. '+(s.run_until?'Stops at '+new Date(s.run_until).toLocaleString()+'. ':'')+'AI reviews each market once.';
  ['cash','equity'].forEach(k=>$(k).textContent=money(a[k]));$('pnl').textContent=money(a.realized_pnl);$('pnl').className=Number(a.realized_pnl)<0?'bad':'good';$('unrealized').textContent=money(a.unrealized_pnl);
  $('window').textContent=m?time(m.open_time)+' – '+time(m.close_time):'Waiting for market';
  $('up').textContent=money(m?.yes_ask_dollars);$('down').textContent=money(m?.no_ask_dollars);
  $('current').textContent='Bitcoin: '+money(u?.price);$('opening').textContent='Opening: '+money(u?.opening_price);$('change').textContent='Change: '+money(u?.delta);
  $('countdown').textContent=m?Math.max(0,(Date.parse(m.close_time)-Date.now())/60000).toFixed(1)+' min left':'—';
  const error=d.errors.BTC||u?.error,stale=!m||Date.now()-Date.parse(m.received_at)>5000||!u?.source_at||Date.now()-Date.parse(u.source_at)>5000;
  $('feed').textContent=error?'Prices unavailable: '+error:stale?'Waiting for fresh prices':u.opening_price==null?'Waiting for opening price':'Prices live · Updated '+time(u.source_at);
  const positions=[...Object.values(s.positions).map(p=>({...p,status:'Open'})),...Object.values(s.pending).map(p=>({...p,status:'Waiting for settlement'}))];
  $('position').innerHTML=positions.length?positions.map(p=>`<div class="event"><strong>${esc(p.side)} · ${esc(p.status)}</strong><br>${esc(p.size)} contracts · Entry ${money(p.entry)} · Current value per contract ${money(p.last_mark)}</div>`).join(''):'No open trade.';
  $('performance').textContent=`${a.trades} closed trades · ${a.wins} wins · ${a.losses} losses`;
  const used=Math.max(0,-Number(a.realized_pnl)),limit=Math.abs(Number(settings.daily_loss));$('lossbudget').textContent=limit?money(used)+' used of '+money(limit):'No loss limit';$('lossbar').style.width=(limit?Math.min(100,used/limit*100):0)+'%';
  const exits=s.events.filter(e=>e.kind==='exit');$('trades').innerHTML=exits.length?exits.slice().reverse().map(e=>`<tr><td>${esc(new Date(e.at).toLocaleString())}</td><td>${esc(e.side)}</td><td>${esc(e.size)}</td><td>${money(e.entry)}</td><td>${money(e.exit)}</td><td class="${Number(e.pnl)<0?'bad':'good'}">${money(e.pnl)}</td></tr>`).join(''):'<tr><td colspan="6">No closed trades yet.</td></tr>';
  const review=d.codex;
  $('reviewstatus').textContent=review?.status==='running'?'AI is reviewing this market…':review?.response?.reason||'No decision yet.';
  $('reviewtime').textContent=review?.payload?.at?'Last review: '+new Date(review.payload.at).toLocaleString():'';
  const login=update.login;$('loginstatus').textContent=login.status;$('codexlogin').hidden=login.authenticated;$('codexlogin').disabled=login.running||Object.values(update.accounts).some(a=>!a.paused);$('loginoutput').textContent=login.authenticated?'':login.output;
}
const stream=new EventSource('/api/events');
stream.onmessage=event=>render(JSON.parse(event.data));
stream.onerror=()=>{
 $('badge').textContent='Reconnecting';
 $('notice').textContent='Live connection interrupted. Reconnecting automatically; displayed values may be out of date.';
 for(const m of ['5','15']){$('start'+m).disabled=true;$('pause'+m).disabled=false}
};
