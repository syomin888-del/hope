'use strict';
(() => {
const $=id=>document.getElementById(id);
const sectorMeta=[
{id:'ingest',title:'Данные и коэффициенты',rgb:[.5,.85,1]},
{id:'sports',title:'Спортивные модели',rgb:[.5,.85,1]},
{id:'evidence',title:'Новости и исследования',rgb:[.5,.85,1]},
{id:'portfolio',title:'Портфель и риск',rgb:[.5,.85,1]},
{id:'settlement',title:'Проверка результатов',rgb:[.5,.85,1]},
{id:'crypto',title:'Криптовалюты',rgb:[.5,.85,1]},
{id:'stocks',title:'Акции и экономика',rgb:[.5,.85,1]},
];
const fmt=x=>x==null?'—':String(x);
const pad=n=>String(n).padStart(2,'0');
const fmtElapsed=v=>v==null?'—':`${pad(Math.floor(v/3600))}:${pad(Math.floor(v%3600/60))}:${pad(Math.floor(v%60))}`;
const fmtWita=x=>{if(!x)return '—';const d=new Date(x);return Number.isNaN(d.getTime())?'—':new Intl.DateTimeFormat('ru-RU',{timeZone:'Asia/Makassar',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(d)};
const node=(tag,className,text)=>{let n=document.createElement(tag);if(className)n.className=className;if(text!=null)n.textContent=String(text);return n};
let last=null,lastReceived=0,selection='all',lastError='',loading=false;
function sectorIndex(id){return Math.max(0,sectorMeta.findIndex(x=>x.id===id))}
function activeLevels(){let levels=Array(7).fill(0);if(last)for(let x of last.active||[]){levels[sectorIndex(x.sector)]=1;}return levels}
function sectorColor(id){let s=sectorMeta[sectorIndex(id)];return `rgb(${s.rgb.map(x=>Math.round(x*255)).join(',')})`}
function statusText(st){return ({COMPLETE:'ГОТОВО',STARTED:'ЗАПУСК',SKIPPED:'НЕ ПО ВРЕМЕНИ',TIMEOUT:'TIMEOUT',FAILED:'ОШИБКА',DEGRADED:'ЧАСТИЧНО',UNKNOWN:'НЕ ПОДТВЕРЖДЁН'})[st]||st}
function updateClocks(){const now=new Date();$('wita').textContent=new Intl.DateTimeFormat('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit',timeZone:'Asia/Makassar'}).format(now)+' WITA';document.querySelectorAll('[data-running-since]').forEach(el=>{let n=Number(el.dataset.runningSince);let base=Number(el.dataset.elapsedBase);el.textContent=fmtElapsed(base+Math.max(0,Math.floor((Date.now()-n)/1000)))})}
function renderSectors(){let root=$('sectors');root.replaceChildren();let levels=activeLevels();for(const [i,s] of sectorMeta.entries()){
 let b=node('button','sector','');b.type='button';b.style.setProperty('--sector',sectorColor(s.id));b.classList.toggle('selected',selection===s.id);b.classList.toggle('live',levels[i]>0);
 let title=node('div','sector-title');title.append(node('span','sector-led'),node('span','',s.title));b.append(title);
 let cnt=(last?.active||[]).filter(x=>x.sector===s.id).length;b.append(node('div','sector-count',cnt?`${cnt} подтвержд. процесс(ов)`:'Нет активных процессов'));
 b.onclick=()=>{selection=selection===s.id?'all':s.id;renderAll()};root.append(b);
}}
function renderActive(){const el=$('active-tasks');el.replaceChildren();let all=(last?.active||[]), unknown=(last?.unconfirmed||[]),shown=selection==='all'?all:all.filter(x=>x.sector===selection);
 if(!shown.length){let empty=node('div','empty-signal');empty.append(node('div','empty-ring','◎'),node('strong','',last?'Нет подтверждённых активных задач':'Ожидаем телеметрию'),node('span','',last?'HOPE может ожидать следующего часового цикла. Мозг не изображает активность, которой не видит.':'Сервер отвечает только за реальные события.'));el.append(empty)}
 for(const item of shown){let box=node('div','task-card');box.style.setProperty('--sector',sectorColor(item.sector));let head=node('div','task-head');head.append(node('strong','',fmt(item.job)),node('span','task-time',fmtElapsed(item.elapsed_seconds)));const timer=head.lastChild;if(item.elapsed_seconds!=null){timer.dataset.runningSince=String(Date.now());timer.dataset.elapsedBase=String(item.elapsed_seconds)}box.append(head,node('div','task-sub',item.action),node('div','task-sub',`PID ${item.pid} · начало ${fmtWita(item.started_at)}`),node('div','task-tag','● PROCESS_VERIFIED / RUNNING'));el.append(box)}
 const unconfirmed=selection==='all'?unknown:unknown.filter(x=>x.sector===selection);
 for(const item of unconfirmed.slice(0,2)){let box=node('div','task-card');box.style.setProperty('--sector','#a5aeb5');box.append(node('strong','',item.job||'V4 / незавершённый журнал'),node('div','task-sub',item.action),node('div','task-tag','◌ UNKNOWN / ПРОЦЕСС НЕ ПОДТВЕРЖДЁН'));el.append(box)}
}
function renderHistory(){let records=(last?.history||[]).filter(x=>selection==='all'||x.sector===selection).slice(0,50);let el=$('history');el.replaceChildren();$('history-count').textContent=records.length+' записей';if(!records.length){el.append(node('div','empty-history','Для этого сектора пока нет сохранённых событий.'));return}
 for(const r of records){let bar=node('span','history-bar');let row=node('div','history-row');row.style.setProperty('--sector',sectorColor(r.sector));let middle=node('div','');middle.append(node('div','history-name',r.description||r.job),node('div','history-meta',`${r.job} · ${r.lane}${r.duration_seconds!=null?' · '+fmtElapsed(r.duration_seconds):''}`));let right=node('div','history-date');right.append(node('span','',fmtWita(r.at)));let status=node('span','history-state '+(r.state==='COMPLETE'?'ok':['FAILED','TIMEOUT','DEGRADED'].includes(r.state)?'bad':'muted'),statusText(r.state));right.append(status);row.append(bar,middle,right);el.append(row)}
}
function renderSelection(){let sec=sectorMeta.find(s=>s.id===selection);$('focus-heading').textContent=sec?sec.title:'Все области мозга';let active=(last?.active||[]).filter(x=>!sec||x.sector===sec.id);let hist=(last?.history||[]).filter(x=>!sec||x.sector===sec.id);let desc=active.length?`Прямо сейчас: ${active.map(x=>x.action).join('; ')}`:hist.length?`Последнее действие: ${hist[0].description}. Результат: ${statusText(hist[0].state)}.`:'Нет подтверждённой активности в доступной истории.';$('focus-desc').textContent=desc;$('focus-counter').textContent=`АКТИВНО ${active.length} · В ИСТОРИИ ${hist.length}`}
function renderAll(){let d=last;let sum=d?.summary||{};$('stat-active').textContent=fmt(sum.running_verified);$('stat-completed').textContent=fmt(sum.completed_24h);$('stat-failed').textContent=fmt(sum.failed_24h);$('stat-sync').textContent=d?fmtWita(d.at_utc).split(' ').pop():'—';let c=$('connection');c.className='connection '+(d?'ok':'bad');c.textContent=d?'● LIVE SOURCE':'● НЕТ СВЯЗИ';if(lastError){c.className='connection bad';c.textContent='● ДАННЫЕ НЕДОСТУПНЫ'}let active=(d?.active||[]).length;$('brain-state').textContent=active?`● ${active} ПРОЦЕСС(ОВ) РАБОТАЮТ`:'◌ НЕТ ПОДТВЕРЖДЁННЫХ ПРОЦЕССОВ';renderSectors();renderActive();renderHistory();renderSelection()}
async function load(){if(loading)return;loading=true;try{let res=await fetch('/api/neural',{cache:'no-store'});if(!res.ok)throw Error('HTTP '+res.status);let d=await res.json();if(d.product!=='HOPE'||!Array.isArray(d.active)||!Array.isArray(d.history))throw Error('BAD_API');last=d;lastReceived=Date.now();lastError='';}catch(e){lastError=String(e.message).slice(0,80); // Do not keep old live animation after a disconnected poll.
 last=null;}finally{loading=false;renderAll()}}


// HOPE V0.7. WebGL is in the locally served, SHA-locked module.
// The existing read-only API and clock calculations remain unchanged.
window.__hopeNeuralState={levels:[],selection:'all',connected:false};
window.__hopeSectorSelected=(sector)=>{
  if(!sectorMeta.some(s=>s.id===sector))return;
  selection=selection===sector?'all':sector;renderAll();
};
const oldRender=renderAll;
renderAll=function(){oldRender();window.__hopeNeuralState={
  levels:activeLevels(),selection,connected:!!last && !lastError,
  active:!lastError?last?.active||[]:[]
};};
const filter=$('history-filter');
for(const s of sectorMeta){const opt=document.createElement('option');opt.value=s.id;opt.textContent=s.title;filter.append(opt)}
filter.addEventListener('change',e=>{selection=e.target.value;renderAll()});
$('refresh').addEventListener('click',load);
setInterval(updateClocks,1000);setInterval(load,10000);updateClocks();load();
})();
