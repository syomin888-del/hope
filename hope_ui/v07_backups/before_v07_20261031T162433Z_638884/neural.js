'use strict';
(() => {
const $=id=>document.getElementById(id);
const sectorMeta=[
{id:'ingest',title:'Данные и коэффициенты',rgb:[.5,.85,1]},
{id:'sports',title:'Спортивные модели',rgb:[.55,.75,1]},
{id:'evidence',title:'Новости и исследования',rgb:[.72,.67,1]},
{id:'portfolio',title:'Портфель и риск',rgb:[.4,.88,.76]},
{id:'settlement',title:'Проверка результатов',rgb:[.94,.71,.56]},
{id:'crypto',title:'Криптовалюты',rgb:[.8,.61,1]},
{id:'stocks',title:'Акции и экономика',rgb:[.88,.76,.53]},
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

// Custom WebGL-1 anatomical cortex mesh. No external scripts, asset fetches or AI-generated "activity".
class Cortex3D{
 constructor(canvas){this.canvas=canvas;this.gl=canvas.getContext('webgl',{alpha:true,antialias:true,premultipliedAlpha:false})||canvas.getContext('experimental-webgl');this.yaw=-.53;this.pitch=.11;this.zoom=1;this.xray=false;this.drag=false;this.lastX=0;this.lastY=0;this.lastT=0;this.reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches;this.seed=18731;if(this.gl){this.prepare();this.bindEvents();this.render(0)}else $('render-status').textContent='WEBGL НЕДОСТУПЕН'}
 random(){this.seed=(this.seed*1664525+1013904223)>>>0;return this.seed/4294967296}
 shader(kind,source){let g=this.gl,s=g.createShader(kind);g.shaderSource(s,source);g.compileShader(s);if(!g.getShaderParameter(s,g.COMPILE_STATUS))throw Error(g.getShaderInfoLog(s));return s}
 program(v,f){let g=this.gl,p=g.createProgram();g.attachShader(p,this.shader(g.VERTEX_SHADER,v));g.attachShader(p,this.shader(g.FRAGMENT_SHADER,f));g.linkProgram(p);if(!g.getProgramParameter(p,g.LINK_STATUS))throw Error(g.getProgramInfoLog(p));return p}
 baseVertex(){return `precision mediump float;attribute vec3 aPosition;attribute vec3 aNormal;attribute float aSector;attribute float aSeed;uniform float uYaw,uPitch,uZoom,uTime,uAspect;uniform float uActive[7];uniform float uSelected;varying float vGlow,vDepth,vSector,vDiff,vSelected,vPhase;varying vec3 vNormal;vec3 rotate(vec3 p){float c=cos(uYaw),s=sin(uYaw);p=vec3(c*p.x+s*p.z,p.y,-s*p.x+c*p.z);c=cos(uPitch);s=sin(uPitch);return vec3(p.x,c*p.y-s*p.z,s*p.y+c*p.z);}void main(){vec3 p=rotate(aPosition);vNormal=normalize(rotate(aNormal));float z=4.0-p.z;gl_Position=vec4(p.x*3.9*uZoom/uAspect,p.y*3.9*uZoom,(z-1.0)*.64,z);vSector=aSector;vGlow=0.0;for(int i=0;i<7;i++){if(abs(float(i)-aSector)<.4)vGlow=uActive[i];}vSelected=abs(aSector-uSelected)<.4?1.0:0.0;vDiff=max(0.0,dot(vNormal,normalize(vec3(-.35,.7,1.0))));vDepth=p.z;vPhase=fract(uTime*(.28+aSeed*.22)+aSeed);gl_PointSize=(3.0+vGlow*9.0)*uZoom*(4.0/z);}`}
 prepare(){const g=this.gl;this.meshProgram=this.program(this.baseVertex(),`precision mediump float;varying float vGlow,vDepth,vSector,vDiff,vSelected,vPhase;varying vec3 vNormal;uniform float uXray;uniform vec3 uColors[7];void main(){vec3 c=vec3(.12,.23,.32);for(int i=0;i<7;i++)if(abs(float(i)-vSector)<.4)c=mix(c,uColors[i],.18+vGlow*.45+vSelected*.17);float fresnel=pow(1.0-abs(dot(normalize(vNormal),normalize(vec3(0.,0.,1.)))),2.4);c=c*(.65+vDiff*.64)+vec3(.32,.55,.69)*fresnel*.42;c+=vGlow*vec3(.14,.29,.40);c+=vSelected*.085;gl_FragColor=vec4(c,uXray>.5?.26:.96);}`);
 this.lineProgram=this.program(this.baseVertex(),`precision mediump float;varying float vGlow,vDepth,vSector,vDiff,vSelected,vPhase;uniform float uXray;uniform vec3 uColors[7];void main(){vec3 c=vec3(.29,.48,.62);for(int i=0;i<7;i++)if(abs(float(i)-vSector)<.4)c=mix(c,uColors[i],.3+vGlow*.7+vSelected*.25);gl_FragColor=vec4(c,clamp((.14+vGlow*.7+vSelected*.16)*(uXray>.5?1.3:1.0),0.0,1.0));}`);
 this.pulseProgram=this.program(this.baseVertex(),`precision mediump float;varying float vGlow,vDepth,vSector,vDiff,vSelected,vPhase;uniform vec3 uColors[7];void main(){if(vGlow<.5)discard;vec2 c=gl_PointCoord-.5;float r=length(c);if(r>.5)discard;vec3 col=vec3(.6,.88,1.);for(int i=0;i<7;i++)if(abs(float(i)-vSector)<.4)col=uColors[i];gl_FragColor=vec4(col, smoothstep(.5,.0,r)*vGlow);}`);
 // Seven observable areas based on spatial position in a 3D physical cortex.
 const sector=(x,y,z)=>{if(y<-.35)return 4;if(y>.40 && z<.1)return 2;if(x<-.36 && z>0)return 0;if(x>.36 && z>.0)return 1;if(z<-.4 && y<.35)return 5;if(x>.15&&y<.38)return 3;return 6};
 const vertices=[],indices=[],pointSets=Array.from({length:7},()=>[]),edges=[],points=[];
 for(let h=0;h<2;h++){let side=h===0?-1:1;let rows=42,cols=88,offset=vertices.length/8;
 for(let i=0;i<=rows;i++){let latitude=-Math.PI/2+.001+(Math.PI-.002)*i/rows;for(let j=0;j<=cols;j++){let longitude=2*Math.PI*j/cols;
  const pos=this.surface(side,latitude,longitude);
  // sector assignments are categorical, not semantic claims about human anatomy.
  let sec=sector(...pos),seed=this.random();vertices.push(...pos,...this.norm(pos,side),sec,seed);
  if(i>2&&i<rows-2)pointSets[sec].push(pos);
 }}for(let i=0;i<rows;i++)for(let j=0;j<cols;j++){let a=offset+i*(cols+1)+j,b=a+cols+1;indices.push(a,b,a+1,b,b+1,a+1)}}
 this.mesh=this.uploadInterleaved(new Float32Array(vertices),new Uint16Array(indices));
 for(let s=0;s<7;s++){let pool=pointSets[s];for(let i=0;i<220;i++){if(pool.length<2)continue;let a=pool[Math.floor(this.random()*pool.length)],b=null,dist=Infinity;for(let k=0;k<25;k++){let q=pool[Math.floor(this.random()*pool.length)],d=Math.hypot(a[0]-q[0],a[1]-q[1],a[2]-q[2]);if(d>.07&&d<.3&&d<dist){dist=d;b=q}}if(!b)continue;const seed=this.random();for(let p of [a,b])edges.push(...p,0,0,1,s,seed);
 if(i%2===0)points.push({a,b,s,seed})}}
 this.lines=this.uploadInterleaved(new Float32Array(edges));this.particles=points;
 }
 surface(side,t,u){let c=Math.cos(t);let fold=1+.035*Math.sin(18*u+4.5*Math.sin(5*t))*.75+.035*Math.cos(24*t+3*Math.sin(u*7))+.018*Math.sin(31*t+11*u);let fissure=1-.035*Math.exp(-Math.pow(Math.cos(u)*Math.cos(t),2)*70);let x=side*(.35+.59*c*Math.cos(u)*fold);let y=.82*Math.sin(t)*fold+.035*Math.sin(u*3)*c;let z=.93*c*Math.sin(u)*fold*fissure;return [x,y,z]}
 norm(p,side){let q=[(p[0]-side*.35)*1.7,p[1]*1.15,p[2]];let m=Math.hypot(...q)||1;return q.map(a=>a/m)}
 uploadInterleaved(vertices,indices){let g=this.gl,o={buffer:g.createBuffer(),count:indices?indices.length:vertices.length/8};g.bindBuffer(g.ARRAY_BUFFER,o.buffer);g.bufferData(g.ARRAY_BUFFER,vertices,g.STATIC_DRAW);if(indices){o.index=g.createBuffer();g.bindBuffer(g.ELEMENT_ARRAY_BUFFER,o.index);g.bufferData(g.ELEMENT_ARRAY_BUFFER,indices,g.STATIC_DRAW)}return o}
 bindAttrib(p,buffer){let g=this.gl;g.useProgram(p);g.bindBuffer(g.ARRAY_BUFFER,buffer.buffer);const attributes=[['aPosition',3,0],['aNormal',3,12],['aSector',1,24],['aSeed',1,28]];for(let [name,size,offset] of attributes){let at=g.getAttribLocation(p,name);if(at<0)continue;g.enableVertexAttribArray(at);g.vertexAttribPointer(at,size,g.FLOAT,false,32,offset)}if(buffer.index)g.bindBuffer(g.ELEMENT_ARRAY_BUFFER,buffer.index)}
 uniforms(p,t){let g=this.gl,uni=(k)=>g.getUniformLocation(p,k);g.uniform1f(uni('uYaw'),this.yaw);g.uniform1f(uni('uPitch'),this.pitch);g.uniform1f(uni('uZoom'),this.zoom);g.uniform1f(uni('uTime'),t/1000);g.uniform1f(uni('uAspect'),this.canvas.width/this.canvas.height);g.uniform1f(uni('uXray'),this.xray?1:0);g.uniform1f(uni('uSelected'),sectorMeta.findIndex(x=>x.id===selection));g.uniform1fv(uni('uActive[0]'),new Float32Array(activeLevels()));g.uniform3fv(uni('uColors[0]'),new Float32Array(sectorMeta.flatMap(s=>s.rgb)))}
 resize(){let dpr=Math.min(devicePixelRatio||1,2),r=this.canvas.getBoundingClientRect(),w=Math.max(1,Math.round(r.width*dpr)),h=Math.max(1,Math.round(r.height*dpr));if(this.canvas.width!==w||this.canvas.height!==h){this.canvas.width=w;this.canvas.height=h}this.gl.viewport(0,0,w,h)}
 render(t){if(!this.gl)return;this.resize();let g=this.gl;if(!this.drag&&!this.reduced)this.yaw+=.00019*Math.min(60,t-(this.lastT||t));this.lastT=t;g.clearColor(0,0,0,0);g.clear(g.COLOR_BUFFER_BIT|g.DEPTH_BUFFER_BIT);g.enable(g.DEPTH_TEST);g.depthFunc(g.LEQUAL);g.disable(g.CULL_FACE);g.enable(g.BLEND);g.blendFunc(g.SRC_ALPHA,g.ONE_MINUS_SRC_ALPHA);
 this.bindAttrib(this.meshProgram,this.mesh);this.uniforms(this.meshProgram,t);g.depthMask(true);g.drawElements(g.TRIANGLES,this.mesh.count,g.UNSIGNED_SHORT,0);
 g.depthMask(false);g.disable(g.DEPTH_TEST);g.blendFunc(g.SRC_ALPHA,g.ONE);
 this.bindAttrib(this.lineProgram,this.lines);this.uniforms(this.lineProgram,t);g.drawArrays(g.LINES,0,this.lines.count);
 if(activeLevels().some(x=>x>0)&&!this.reduced){let verts=[];for(let p of this.particles){if(!activeLevels()[p.s])continue;let time=(t*.00024*(.5+p.seed)+p.seed)%1,pos=p.a.map((x,i)=>x*(1-time)+p.b[i]*time);verts.push(...pos,0,0,1,p.s,p.seed)}if(verts.length){let buf=this.uploadInterleaved(new Float32Array(verts));this.bindAttrib(this.pulseProgram,buf);this.uniforms(this.pulseProgram,t);g.drawArrays(g.POINTS,0,buf.count);g.deleteBuffer(buf.buffer)}}g.depthMask(true);requestAnimationFrame(x=>this.render(x))}
 bindEvents(){let c=this.canvas;c.addEventListener('pointerdown',e=>{this.drag=true;this.lastX=e.clientX;this.lastY=e.clientY;c.setPointerCapture(e.pointerId)});c.addEventListener('pointermove',e=>{if(!this.drag)return;this.yaw+=(e.clientX-this.lastX)*.008;this.pitch=Math.max(-.9,Math.min(.9,this.pitch+(e.clientY-this.lastY)*.006));this.lastX=e.clientX;this.lastY=e.clientY});c.addEventListener('pointerup',()=>this.drag=false);c.addEventListener('pointercancel',()=>this.drag=false);c.addEventListener('wheel',e=>{e.preventDefault();this.zoom=Math.max(.65,Math.min(1.75,this.zoom-e.deltaY*.001))},{passive:false});$('reset').addEventListener('click',()=>{this.yaw=-.53;this.pitch=.11;this.zoom=1});$('xray').addEventListener('click',()=>{this.xray=!this.xray;$('xray').setAttribute('aria-pressed',String(this.xray))})}
}

// Safe software-rendered fallback for browsers where WebGL is unavailable.
// The underlying positions are 3D and retain yaw/pitch drag and zoom.
class CortexFallback{
 constructor(canvas){this.c=canvas;this.ctx=canvas.getContext('2d');this.yaw=-.53;this.pitch=.11;this.zoom=1;this.drag=false;this.tprev=0;this.reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches;this.nodes=[];this.edges=[];this.folds=[];let seed=13072;let rand=()=>{seed=(Math.imul(1664525,seed)+1013904223)>>>0;return seed/4294967296};
 const sec=(x,y,z)=>y<-.35?4:y>.4&&z<.1?2:x<-.36&&z>0?0:x>.36&&z>0?1:z<-.4&&y<.35?5:x>.15&&y<.38?3:6;
 const surf=(side,t,u)=>{let c=Math.cos(t),f=1+.04*Math.sin(18*u+4.5*Math.sin(5*t))+.026*Math.cos(24*t+3*Math.sin(u*7));return[side*(.35+.59*c*Math.cos(u)*f),.82*Math.sin(t)*f,.93*c*Math.sin(u)*f]};
 for(let side of [-1,1]){
  for(let i=0;i<650;i++){let t=Math.asin(rand()*1.96-.98),u=rand()*Math.PI*2,p=surf(side,t,u);this.nodes.push({p,s:sec(...p)})}
  for(let j=0;j<38;j++){let u=j*Math.PI/19,path=[];for(let i=0;i<=32;i++)path.push(surf(side,(i/32-.5)*Math.PI*.95,u));this.folds.push(path)}
 }
 for(let i=0;i<this.nodes.length;i++){
  let a=this.nodes[i],best=-1,dist=1;
  for(let k=0;k<27;k++){let j=Math.floor(rand()*this.nodes.length),b=this.nodes[j];if(a.s!==b.s||i===j)continue;let d=Math.hypot(a.p[0]-b.p[0],a.p[1]-b.p[1],a.p[2]-b.p[2]);if(d>.05&&d<.28&&d<dist){dist=d;best=j}}
  if(best>=0)this.edges.push({i,j:best,seed:rand(),speed:.4+rand()*.8,s:a.s});
 }
 this.bind();requestAnimationFrame(t=>this.frame(t));$('render-status').textContent='SOFTWARE 3D';
 }
 bind(){let c=this.c,drag=false,x=0,y=0;c.addEventListener('pointerdown',e=>{drag=true;this.drag=true;x=e.clientX;y=e.clientY;c.setPointerCapture(e.pointerId)});c.addEventListener('pointermove',e=>{if(!drag)return;this.yaw+=(e.clientX-x)*.008;this.pitch=Math.max(-.8,Math.min(.8,this.pitch+(e.clientY-y)*.007));x=e.clientX;y=e.clientY});c.addEventListener('pointerup',()=>{drag=false;this.drag=false});c.addEventListener('pointercancel',()=>{drag=false;this.drag=false});c.addEventListener('wheel',e=>{e.preventDefault();this.zoom=Math.max(.65,Math.min(1.7,this.zoom-e.deltaY*.001))},{passive:false});$('reset').onclick=()=>{this.yaw=-.53;this.pitch=.11;this.zoom=1};$('xray').onclick=()=>{this.xray=!this.xray;$('xray').setAttribute('aria-pressed',String(!!this.xray))}}
 project(p,w,h){let c=Math.cos(this.yaw),s=Math.sin(this.yaw),x=p[0]*c+p[2]*s,z=-p[0]*s+p[2]*c,cc=Math.cos(this.pitch),ss=Math.sin(this.pitch),y=p[1]*cc-z*ss;z=p[1]*ss+z*cc;let q=(Math.min(w*.33,h*.5)*this.zoom)*4/(4-z);return{x:w*.5+x*q,y:h*.52-y*q,z}}
 frame(t){const c=this.c,ctx=this.ctx,dpr=Math.min(devicePixelRatio||1,2),r=c.getBoundingClientRect(),w=Math.max(1,r.width),h=Math.max(1,r.height);if(c.width!==Math.round(w*dpr)||c.height!==Math.round(h*dpr)){c.width=Math.round(w*dpr);c.height=Math.round(h*dpr)}ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);if(!this.drag&&!this.reduced)this.yaw+=.00015*Math.min(60,t-(this.tprev||t));this.tprev=t;let proj=this.nodes.map(n=>this.project(n.p,w,h)),active=activeLevels();
 const sel=sectorMeta.findIndex(x=>x.id===selection);ctx.save();ctx.globalCompositeOperation='screen';
 let order=this.edges.map(e=>({...e,d:(proj[e.i].z+proj[e.j].z)/2})).sort((a,b)=>a.d-b.d);
 for(let e of order){let a=proj[e.i],b=proj[e.j],f=Math.max(.18,(e.d+1.4)/2.8);ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);let color=sectorMeta[e.s].rgb;let q=x=>Math.round(x*255);let light=active[e.s]||e.s===sel;ctx.strokeStyle=`rgba(${q(color[0])},${q(color[1])},${q(color[2])},${f*(light?.37:.12)})`;ctx.lineWidth=light?1.0:.6;ctx.stroke()}
 for(let path of this.folds){let points=path.map(p=>this.project(p,w,h));ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y));ctx.strokeStyle='rgba(145,201,233,.22)';ctx.lineWidth=.8;ctx.stroke()}
 for(let i=0;i<this.nodes.length;i++){let p=proj[i],s=this.nodes[i].s,f=Math.max(.1,(p.z+1.4)/2.8),on=active[s]>0;ctx.beginPath();ctx.arc(p.x,p.y,on?1.3:.65,0,2*Math.PI);ctx.fillStyle=on?`rgba(150,224,255,${.8*f})`:`rgba(111,165,199,${f*.5})`;ctx.fill()}
 if(active.some(Boolean)&&!this.reduced){ctx.shadowBlur=14;ctx.shadowColor='#8edfff';for(let e of order){if(!active[e.s])continue;let a=proj[e.i],b=proj[e.j],v=(t*.0002*e.speed+e.seed)%1;ctx.beginPath();ctx.arc(a.x+(b.x-a.x)*v,a.y+(b.y-a.y)*v,1.8,0,2*Math.PI);ctx.fillStyle='#a9e8ff';ctx.fill()}}
 ctx.restore();requestAnimationFrame(x=>this.frame(x))}
}

const filter=$('history-filter');for(const s of sectorMeta){let opt=document.createElement('option');opt.value=s.id;opt.textContent=s.title;filter.append(opt)}filter.addEventListener('change',e=>{selection=e.target.value;renderAll()});$('refresh').addEventListener('click',load);
try{let cortex=new Cortex3D($('brain3d'));if(!cortex.gl)new CortexFallback($('brain3d'))}catch(e){$('render-status').textContent='3D FALLBACK';console.warn('HOPE Neural WebGL unavailable:',e.message);try{new CortexFallback($('brain3d'))}catch(e2){$('render-status').textContent='ГРАФИКА НЕДОСТУПНА'}}setInterval(updateClocks,1000);setInterval(load,10000);updateClocks();load();
})();
