"""Interactive code-network explorer: one self-contained HTML file.

``incidentlens graph`` renders every scanned service's code graph into a
single dark-theme HTML page — force-directed network on a canvas, zoom and
pan, search, a service switcher, and a details panel that answers the on-call
questions directly: click a module and see **who calls it** and **what it
calls**, with the symbols involved. When an incident analysis is supplied,
the traversed module path glows teal, a log-confirmed module burns red, and a
static-only module or function attribution is amber.

No CDN, no network: the JavaScript is embedded, the data is inlined JSON.
The file can be attached to an incident channel like the video.
"""

from __future__ import annotations

import json
from pathlib import Path

from incidentlens.domain.models import CodeGraph, IncidentAnalysis
from incidentlens.studio.evidence import module_failure_is_log_confirmed

_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>IncidentLens · code network</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0e1116;--panel:#151a22;--panel2:#1b212b;--line:#263042;--text:#e6e9ef;
--dim:#8a93a3;--accent:#8ca8da;--ok:#3fb68b;--warn:#e3a73c;--crit:#e05b4d;--rec:#52c7b8;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,'Segoe UI',sans-serif;overflow:hidden}
#wrap{display:flex;height:100vh}
#side{width:340px;min-width:340px;background:var(--panel);border-right:1px solid var(--line);
display:flex;flex-direction:column;padding:14px;gap:10px;overflow-y:auto}
h1{font-size:16px} h1 small{color:var(--dim);font-weight:400;display:block;font-size:11px;margin-top:2px}
select,input{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--text);
padding:7px 9px;border-radius:8px;font:inherit}
.row{display:flex;gap:8px}
button{background:var(--panel2);border:1px solid var(--line);color:var(--dim);padding:6px 10px;
border-radius:8px;cursor:pointer;font:12px monospace}
button.on{color:var(--bg);background:var(--accent);border-color:var(--accent)}
#stats{color:var(--dim);font:11px monospace}
#legend{display:flex;flex-wrap:wrap;gap:6px;font:11px monospace;color:var(--dim)}
.chip{display:flex;align-items:center;gap:5px}.dot{width:9px;height:9px;border-radius:50%}
#detail{border-top:1px solid var(--line);padding-top:10px;font-size:13px}
#detail h2{font:600 14px monospace;word-break:break-all}
#detail .k{color:var(--dim);font:11px monospace;margin:8px 0 3px;text-transform:uppercase;letter-spacing:.06em}
#detail ul{list-style:none}
#detail li{padding:4px 6px;border-radius:6px;cursor:pointer;font:12px monospace;word-break:break-all}
#detail li:hover{background:var(--panel2)}
#detail li span{color:var(--dim)}
.badge{display:inline-block;padding:1px 8px;border-radius:9px;font:11px monospace;margin:2px 3px 2px 0;
background:var(--panel2);border:1px solid var(--line);color:var(--dim)}
.badge.crit{color:var(--crit);border-color:var(--crit)}
.badge.rec{color:var(--rec);border-color:var(--rec)}
#inc{border:1px solid var(--line);border-radius:10px;padding:9px;font-size:12px;color:var(--dim)}
#inc b{color:var(--text)}
#stage{width:100%;height:100%;position:relative}
canvas{display:block;cursor:grab}
#tip{position:absolute;pointer-events:none;background:var(--panel2);border:1px solid var(--line);
color:var(--text);padding:4px 9px;border-radius:7px;font:12px monospace;display:none;z-index:3}
</style></head><body>
<div id="wrap">
  <div id="side">
    <h1>IncidentLens · code network<small id="sub"></small></h1>
    <select id="svc"></select>
    <input id="search" placeholder="search modules… (e.g. pii, llm, guardrail)">
    <div class="row">
      <button id="levelbtn" style="display:none">show symbols</button>
      <button id="incbtn" style="display:none">incident overlay</button>
      <button id="reset">reset view</button>
    </div>
    <div id="stats"></div>
    <div id="legend"></div>
    <div id="inc" style="display:none"></div>
    <div id="detail"><div style="color:var(--dim)">Click a module to see who calls it and what it calls.</div></div>
  </div>
  <div id="stage"><canvas id="cv"></canvas><div id="tip"></div></div>
</div>
<script>
const DATA = __DATA__;
const KIND_COLOR = {endpoint:'#8ca8da','graph-node':'#3fb68b',client:'#e3a73c',
middleware:'#a78bda',config:'#52c7b8',module:'#55627e',logic:'#6b7794',test:'#8a6a6a',
function:'#6b7794','async-function':'#6b7794',method:'#7f8bab','async-method':'#7f8bab',
class:'#c9a24a'};
const cv=document.getElementById('cv'),ctx=cv.getContext('2d'),tip=document.getElementById('tip');
const stage=document.getElementById('stage');
let G=null,svcName=null,level='module',nodes=[],links=[],byName={},sel=null,hover=null,
  zoom=1,ox=0,oy=0,alpha=0,overlay=!!DATA.incident,query='';
function resize(){cv.width=stage.clientWidth;cv.height=stage.clientHeight;draw();}
window.addEventListener('resize',resize);

const MAX_SYM=800;
function loadService(name){svcName=name;G=DATA.services[name];rebuild(true);}
function rebuild(resetView){
  byName={};sel=null;hover=null;query='';document.getElementById('search').value='';
  const useSym=level==='symbol'&&G.symbols&&G.symbols.length;
  let items=useSym?G.symbols:G.modules;
  let rawEdges=useSym?(G.symbol_edges||[]):G.edges;
  const idOf=useSym?(x=>x.qualname):(x=>x.name);
  if(useSym&&items.length>MAX_SYM){                 // keep the most-connected symbols
    const d={};rawEdges.forEach(e=>{d[e.src]=(d[e.src]||0)+1;d[e.dst]=(d[e.dst]||0)+1;});
    items=[...items].sort((a,b)=>(d[b.qualname]||0)-(d[a.qualname]||0)).slice(0,MAX_SYM);
    const keep=new Set(items.map(idOf));
    rawEdges=rawEdges.filter(e=>keep.has(e.src)&&keep.has(e.dst));
  }
  const cycleSet=new Set([].concat(...((useSym?G.symbol_cycles:G.cycles)||[])));
  const deg={};rawEdges.forEach(e=>{deg[e.src]=(deg[e.src]||0)+1;deg[e.dst]=(deg[e.dst]||0)+1;});
  const maxBlast=Math.max(1,...G.modules.map(m=>m.blast_radius||0));
  nodes=items.map((m,i)=>{const a=i/items.length*6.283;
    const r=Math.min(cv.width,cv.height)*.33*(0.55+((i*2654435761)>>>8&255)/255*.6);
    const id=idOf(m);
    const color=useSym?(KIND_COLOR[m.role]||KIND_COLOR[m.kind]||KIND_COLOR.logic)
                      :(KIND_COLOR[m.kind]||KIND_COLOR.module);
    const size=useSym?1:(0.6+1.7*((m.blast_radius||0)/maxBlast));
    const label=useSym?(m.name+(m.kind==='class'||m.kind==='module'?'':'()')):id.split('.').pop();
    return {...m,name:id,label,color,size,cycle:cycleSet.has(id),deg:deg[id]||0,
            x:Math.cos(a)*r,y:Math.sin(a)*r,vx:0,vy:0};});
  nodes.forEach(n=>byName[n.name]=n);
  links=rawEdges.filter(e=>byName[e.src]&&byName[e.dst]).map(e=>({...e,a:byName[e.src],b:byName[e.dst]}));
  const calls=rawEdges.filter(e=>e.kind==='call'||e.kind==='construct').length;
  document.getElementById('stats').textContent=useSym
    ?(items.length+' symbols · '+rawEdges.length+' calls'+(cycleSet.size?' · '+cycleSet.size+' in cycles':''))
    :(items.length+' modules · '+rawEdges.length+' edges ('+calls+' resolved calls'+
      (cycleSet.size?', '+(G.cycles||[]).length+' cycles':'')+')');
  if(resetView){zoom=1;ox=0;oy=0;}
  alpha=1;detail(null);
  const lb=document.getElementById('levelbtn');
  if(lb){lb.textContent=useSym?'show modules':'show symbols';lb.classList.toggle('on',useSym);
    lb.style.display=(G.symbols&&G.symbols.length)?'':'none';}
  const inc=DATA.incident;
  document.getElementById('incbtn').style.display=(inc&&inc.service===svcName)?'':'none';
  document.getElementById('inc').style.display=(inc&&inc.service===svcName&&overlay)?'':'none';
  if(inc&&inc.service===svcName){
    const inferred=useSym||!inc.module_failure_confirmed;
    const what=useSym?'candidate function':(inferred?'attributed module':'failure logged in module');
    const who=useSym?(inc.failing_symbol||inc.failing_module):(inc.failing_module);
    document.getElementById('inc').innerHTML=
      '<b>'+inc.title+'</b><br>'+inc.incident_id+'<br>'+what+': '+
      '<b style="color:'+(inferred?'var(--warn)':'var(--crit)')+'">'+(who||'?')+'</b>'+
      (!useSym&&inc.failing_symbol?'<br><span style="color:var(--dim)">fn: '+
        inc.failing_symbol.split('.').pop()+' · static candidate</span>':'');}
}
function isPath(n){const i=DATA.incident;if(!(overlay&&i&&i.service===svcName))return false;
  return level==='symbol'?false:i.path_modules.includes(n.name);}
function isFail(n){const i=DATA.incident;if(!(overlay&&i&&i.service===svcName))return false;
  return (level==='symbol'?i.failing_symbol:i.failing_module)===n.name;}
function locusColor(){const i=DATA.incident;
  return level==='symbol'||!(i&&i.module_failure_confirmed)?'#e3a73c':'#e05b4d';}
function neighbors(n){const s=new Set();links.forEach(l=>{if(l.a===n)s.add(l.b);if(l.b===n)s.add(l.a);});return s;}

function tick(){
  if(alpha<=0.003)return;
  const k=alpha;
  for(let i=0;i<nodes.length;i++){const n=nodes[i];
    for(let j=i+1;j<nodes.length;j++){const m=nodes[j];
      let dx=n.x-m.x,dy=n.y-m.y,d2=dx*dx+dy*dy+40;if(d2>90000)continue;
      const f=1600/d2*k;const d=Math.sqrt(d2);dx/=d;dy/=d;
      n.vx+=dx*f;n.vy+=dy*f;m.vx-=dx*f;m.vy-=dy*f;}}
  links.forEach(l=>{let dx=l.b.x-l.a.x,dy=l.b.y-l.a.y;const d=Math.sqrt(dx*dx+dy*dy)+1e-3;
    const want=70+(l.kind==='import'?36:0);const f=(d-want)*0.02*k;dx/=d;dy/=d;
    l.a.vx+=dx*f*d*0.02;l.a.vy+=dy*f*d*0.02;l.b.vx-=dx*f*d*0.02;l.b.vy-=dy*f*d*0.02;});
  nodes.forEach(n=>{n.vx-=n.x*0.0016*k;n.vy-=n.y*0.0016*k;
    n.x+=n.vx=Math.max(-9,Math.min(9,n.vx*0.86));n.y+=n.vy=Math.max(-9,Math.min(9,n.vy*0.86));});
  alpha*=0.995;
}
function nodeR(n){return (4+Math.min(11,Math.sqrt(n.deg)*1.9))*(n.size||1);}
function toScreen(x,y){return [cv.width/2+(x+ox)*zoom, cv.height/2+(y+oy)*zoom];}

function draw(){
  ctx.clearRect(0,0,cv.width,cv.height);
  if(!G)return;
  const focus=sel||hover;const nb=focus?neighbors(focus):null;
  const q=query.toLowerCase();
  ctx.lineWidth=1;
  links.forEach(l=>{
    const[a1,a2]=toScreen(l.a.x,l.a.y),[b1,b2]=toScreen(l.b.x,l.b.y);
    let al=l.kind==='call'?0.34:0.13;
    if(focus){al=(l.a===focus||l.b===focus)?0.75:0.05;}
    let color='#39435a';
    if(overlay&&isFail(l.b)&&isPath(l.a))color=locusColor();
    if(focus&&(l.a===focus||l.b===focus))color='#8ca8da';
    ctx.strokeStyle=color;ctx.globalAlpha=al;
    ctx.beginPath();ctx.moveTo(a1,a2);
    ctx.quadraticCurveTo((a1+b1)/2+(a2-b2)*0.08,(a2+b2)/2+(b1-a1)*0.08,b1,b2);ctx.stroke();
    if(focus&&(l.a===focus||l.b===focus)){
      const t=0.82,x=a1+(b1-a1)*t,y=a2+(b2-a2)*t,ang=Math.atan2(b2-a2,b1-a1);
      ctx.globalAlpha=0.9;ctx.fillStyle=color;ctx.beginPath();
      ctx.moveTo(x,y);ctx.lineTo(x-8*Math.cos(ang-0.4),y-8*Math.sin(ang-0.4));
      ctx.lineTo(x-8*Math.cos(ang+0.4),y-8*Math.sin(ang+0.4));ctx.fill();}
  });
  ctx.globalAlpha=1;
  nodes.forEach(n=>{
    const[x,y]=toScreen(n.x,n.y);const r=nodeR(n)*Math.pow(zoom,0.6);
    const matches=q&&n.name.toLowerCase().includes(q);
    let dim=(focus&&n!==focus&&!nb.has(n))||(q&&!matches);
    let color=n.color||KIND_COLOR[n.kind]||KIND_COLOR.module;
    if(overlay){if(isFail(n))color=locusColor();else if(isPath(n))color='#52c7b8';}
    if(overlay&&isFail(n)){ctx.globalAlpha=0.25;ctx.fillStyle=color;
      ctx.beginPath();ctx.arc(x,y,r+9+3*Math.sin(Date.now()/300),0,6.283);ctx.fill();}
    ctx.globalAlpha=dim?0.16:1;
    ctx.fillStyle=color;ctx.beginPath();ctx.arc(x,y,r,0,6.283);ctx.fill();
    if(n===sel){ctx.strokeStyle='#e6e9ef';ctx.lineWidth=2;ctx.stroke();}
    if(overlay&&isPath(n)&&!isFail(n)){ctx.strokeStyle='#52c7b8';ctx.lineWidth=1.5;
      ctx.beginPath();ctx.arc(x,y,r+3,0,6.283);ctx.stroke();}
    if(n.cycle&&!(overlay&&(isFail(n)||isPath(n)))){ctx.save();ctx.strokeStyle='#e3a73c';
      ctx.lineWidth=1.5;ctx.setLineDash([3,3]);ctx.beginPath();ctx.arc(x,y,r+3,0,6.283);
      ctx.stroke();ctx.restore();}
    const showLabel=!dim&&(n.deg>=8||zoom>1.5||n===focus||(nb&&nb.has(n))||matches||isFail(n));
    if(showLabel){ctx.globalAlpha=dim?0.3:0.92;ctx.fillStyle='#c9d1de';
      ctx.font=(n===focus?'600 ':'')+Math.max(10,11*Math.pow(zoom,0.4))+'px monospace';
      ctx.fillText(n.label||n.name.split('.').pop(),x+r+4,y+4);}
  });
  ctx.globalAlpha=1;
}
function loop(){tick();draw();requestAnimationFrame(loop);}

function detail(n){
  const d=document.getElementById('detail');
  if(!n){d.innerHTML='<div style="color:var(--dim)">Click a node to see who calls it and what it calls.</div>';return;}
  const callers=[],callees=[];
  links.forEach(l=>{if(l.b===n)callers.push(l);if(l.a===n)callees.push(l);});
  const li=(l,other)=>'<li data-m="'+other.name+'">'+other.name+
    (l.symbols&&l.symbols.length?' <span>· '+l.symbols.slice(0,4).join(', ')+'</span>':'')+
    (l.kind==='import'?' <span>(import)</span>':l.kind==='dynamic'?' <span>(dynamic import)</span>':
     l.kind==='construct'?' <span>(constructs)</span>':'')+'</li>';
  const sym=level==='symbol';
  d.innerHTML='<h2>'+n.name+'</h2>'
    +'<span class="badge">'+n.kind+'</span>'
    +(n.role&&n.role!==n.kind?'<span class="badge">'+n.role+'</span>':'')
    +(n.stage?'<span class="badge rec">stage: '+n.stage+'</span>':'')
    +(isFail(n)?'<span class="badge crit">failing '+(sym?'function':'module')+'</span>':'')
    +(n.cycle?'<span class="badge" style="color:var(--warn);border-color:var(--warn)">in cycle</span>':'')
    +(!sym&&typeof n.blast_radius==='number'?'<span class="badge">blast '+n.blast_radius+'</span>':'')
    +'<span class="badge">'+(n.loc||0)+' loc</span>'
    +(sym&&n.module?'<div class="k">module</div><div style="font:12px monospace;color:var(--dim);word-break:break-all">'+n.module+'</div>':'')
    +(!sym&&n.defs&&n.defs.length?'<div class="k">defines</div><div style="font:12px monospace;color:var(--dim)">'
      +n.defs.slice(0,10).join(', ')+'</div>':'')
    +'<div class="k">called / used by ('+callers.length+')</div><ul>'
    +(callers.map(l=>li(l,l.a)).join('')||'<li><span>nothing internal</span></li>')+'</ul>'
    +'<div class="k">calls / uses ('+callees.length+')</div><ul>'
    +(callees.map(l=>li(l,l.b)).join('')||'<li><span>nothing internal</span></li>')+'</ul>';
  d.querySelectorAll('li[data-m]').forEach(el=>el.addEventListener('click',()=>{
    const t=byName[el.getAttribute('data-m')];if(t){sel=t;detail(t);draw();}}));
}

const svcSel=document.getElementById('svc');
Object.keys(DATA.services).forEach(s=>{const o=document.createElement('option');o.value=s;
  o.textContent=s+'  ('+DATA.services[s].modules.length+' modules)';svcSel.appendChild(o);});
svcSel.addEventListener('change',()=>loadService(svcSel.value));
document.getElementById('search').addEventListener('input',e=>{query=e.target.value;draw();});
document.getElementById('reset').addEventListener('click',()=>{zoom=1;ox=0;oy=0;alpha=0.6;});
const incbtn=document.getElementById('incbtn');
function syncInc(){incbtn.classList.toggle('on',overlay);
  const i=DATA.incident;document.getElementById('inc').style.display=
  (i&&i.service===svcSel.value&&overlay)?'':'none';}
incbtn.addEventListener('click',()=>{overlay=!overlay;syncInc();draw();});
const levelbtn=document.getElementById('levelbtn');
levelbtn.addEventListener('click',()=>{level=(level==='symbol')?'module':'symbol';rebuild(false);});

let dragging=false,dragNode=null,lx=0,ly=0;
function pick(mx,my){for(let i=nodes.length-1;i>=0;i--){const n=nodes[i];
  const[x,y]=toScreen(n.x,n.y);const r=nodeR(n)*Math.pow(zoom,0.6)+3;
  if((mx-x)**2+(my-y)**2<r*r)return n;}return null;}
cv.addEventListener('mousedown',e=>{lx=e.offsetX;ly=e.offsetY;
  dragNode=pick(e.offsetX,e.offsetY);dragging=!dragNode;cv.style.cursor='grabbing';});
window.addEventListener('mouseup',()=>{dragging=false;dragNode=null;cv.style.cursor='grab';});
cv.addEventListener('mousemove',e=>{
  if(dragNode){dragNode.x+=(e.offsetX-lx)/zoom;dragNode.y+=(e.offsetY-ly)/zoom;
    alpha=Math.max(alpha,0.12);lx=e.offsetX;ly=e.offsetY;draw();return;}
  if(dragging){ox+=(e.offsetX-lx)/zoom;oy+=(e.offsetY-ly)/zoom;lx=e.offsetX;ly=e.offsetY;draw();return;}
  const n=pick(e.offsetX,e.offsetY);hover=n;
  if(n){tip.style.display='block';tip.style.left=(e.offsetX+14)+'px';tip.style.top=(e.offsetY+8)+'px';
    tip.textContent=n.name;}else tip.style.display='none';
  draw();});
cv.addEventListener('click',e=>{const n=pick(e.offsetX,e.offsetY);
  if(n){sel=n;detail(n);}else{sel=null;detail(null);}draw();});
cv.addEventListener('wheel',e=>{e.preventDefault();
  const f=e.deltaY<0?1.12:0.89;const nz=Math.max(0.25,Math.min(6,zoom*f));
  const mx=e.offsetX-cv.width/2,my=e.offsetY-cv.height/2;
  ox-=mx/zoom-mx/nz;oy-=my/zoom-my/nz;zoom=nz;draw();},{passive:false});

const legend=document.getElementById('legend');
[['endpoint',KIND_COLOR.endpoint],['client',KIND_COLOR.client],['config',KIND_COLOR.config],
 ['middleware',KIND_COLOR.middleware],['graph-node',KIND_COLOR['graph-node']],
 ['class',KIND_COLOR.class],['method',KIND_COLOR.method],['logic',KIND_COLOR.logic]]
 .forEach(([k,c])=>{legend.innerHTML+=
  '<span class="chip"><span class="dot" style="background:'+c+'"></span>'+k+'</span>';});
legend.innerHTML+='<span class="chip"><span class="dot" style="border:1.5px dashed #e3a73c"></span>cycle</span>'
  +'<span class="chip"><span class="dot" style="background:#e05b4d"></span>failure logged</span>'
  +'<span class="chip"><span class="dot" style="background:#e3a73c"></span>static candidate</span>'
  +'<span class="chip"><span class="dot" style="background:#52c7b8"></span>incident path</span>';
document.getElementById('sub').textContent=DATA.subtitle;

const first=DATA.incident&&DATA.services[DATA.incident.service]?DATA.incident.service
  :Object.keys(DATA.services)[0];
svcSel.value=first;resize();loadService(first);syncInc();loop();
</script></body></html>
"""


def incident_overlay(analysis: IncidentAnalysis, graphs: dict[str, CodeGraph]) -> dict | None:
    """Overlay payload: traversed modules + failing module, from the trace."""
    trace = analysis.internal_trace
    if trace is None or trace.service not in graphs:
        return None
    graph = graphs[trace.service]
    known = {m.name for m in graph.modules}

    # stage -> modules mapping comes through the stages the trace names
    stage_modules: dict[str, list[str]] = {}
    for module in graph.modules:
        if module.stage:
            stage_modules.setdefault(module.stage, []).append(module.name)

    path_modules: list[str] = []
    for stage in trace.path:
        path_modules.extend(stage_modules.get(stage, []))
    failing = trace.failing_module
    if failing is None and trace.failing_stage:
        candidates = stage_modules.get(trace.failing_stage, [])
        failing = candidates[0] if candidates else None
    known_syms = {s.qualname for s in graph.symbols}
    return {
        "service": trace.service,
        "failing_module": failing if failing in known else None,
        "failing_symbol": trace.failing_symbol if trace.failing_symbol in known_syms else None,
        "module_failure_confirmed": module_failure_is_log_confirmed(trace, analysis),
        "path_modules": [m for m in path_modules if m in known],
        "title": analysis.title,
        "incident_id": analysis.incident_id,
    }


def render_code_graph_html(
    graphs: dict[str, CodeGraph],
    out_path: str | Path,
    *,
    analysis: IncidentAnalysis | None = None,
    subtitle: str = "who calls what, from static analysis — click around",
) -> Path:
    if not graphs:
        raise ValueError("no code graphs to render")
    payload = {
        "subtitle": subtitle,
        "incident": incident_overlay(analysis, graphs) if analysis else None,
        "services": {
            name: {
                "modules": [m.model_dump() for m in g.modules],
                "edges": [e.model_dump() for e in g.edges],
                "symbols": [s.model_dump() for s in g.symbols],
                "symbol_edges": [e.model_dump() for e in g.symbol_edges],
                "cycles": g.cycles,
                "symbol_cycles": g.symbol_cycles,
            }
            for name, g in graphs.items()
        },
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    html = _TEMPLATE.replace("__DATA__", json.dumps(payload))
    out.write_text(html, encoding="utf-8")
    return out
