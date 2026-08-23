"""Draft-night HTML fallback (LS-37): one self-contained page that polls ``/draft/{id}/state``.

No build step, no external assets, no logic the API doesn't already have: the page is a static
shell whose small script fetches the state document every few seconds and redraws the clock strip,
my needs and the best-available table (``pick_score`` order, tier / cliff / run / survival
columns). Position buttons filter client-side. If the draft isn't running yet the page offers the
**start** button (``POST /draft/{id}/start``), so draft night needs a browser and nothing else.
Readable on a phone or beside the Sleeper room on a second monitor; dark like ``/board.html``.
"""

# ruff: noqa: E501 — the inline CSS/JS is deliberately one-liner-dense
from __future__ import annotations

import json
from html import escape

_CSS = """
body{font:14px/1.35 system-ui,Segoe UI,Roboto,sans-serif;margin:0;background:#111;color:#e6e6e6}
header{padding:10px 14px;background:#1b1b1b;position:sticky;top:0;border-bottom:1px solid #333;z-index:2}
h1{font-size:16px;margin:0 0 6px}h1 small,small{color:#999;font-weight:normal}
a{color:#93c5fd}
.clock{display:flex;flex-wrap:wrap;gap:6px 18px;align-items:baseline;margin:4px 0}
.clock b{font-size:18px}.turn{color:#fde047;font-weight:bold}
.needs span{display:inline-block;margin:2px 6px 0 0;padding:0 6px;border-radius:3px;background:#222;
 border:1px solid #444;font-size:12px}.needs span.hot{border-color:#f59e0b;color:#fde68a}
.filters button{margin:4px 4px 0 0;padding:4px 10px;border:1px solid #444;background:#222;
 color:#ddd;border-radius:4px;cursor:pointer}.filters button.on{background:#3b82f6;color:#fff}
.filters button.start{background:#16a34a;color:#fff;border-color:#16a34a}
.status{font-size:12px;color:#999;margin-top:4px}.status.err{color:#fca5a5}
.banner{background:#7f1d1d;color:#fecaca;padding:6px 14px;display:none}.banner.on{display:block}
table{border-collapse:collapse;width:100%}th,td{padding:4px 8px;text-align:right;
 border-bottom:1px solid #2a2a2a;white-space:nowrap}th{background:#1b1b1b}
td.l,th.l{text-align:left}tr.cliff td{border-bottom:2px solid #ef4444}
.tag{display:inline-block;padding:0 6px;border-radius:3px;font-size:12px;margin-left:4px}
.value{background:#14532d;color:#bbf7d0}.reach{background:#7f1d1d;color:#fecaca}
.run{background:#7c2d12;color:#fed7aa}.cliffTag{background:#ef4444;color:#fff}
.disagree{background:#713f12;color:#fde68a}
tr.t-odd td{background:#161616}tr.mine td{background:#1e3a8a}
.surv{display:inline-block;min-width:34px}.surv.lo{color:#f87171}.surv.hi{color:#86efac}
@media (max-width:640px){th.m,td.m{display:none}body{font-size:13px}th,td{padding:3px 5px}}
"""

_JS = """
const DID=__DID__,LIMIT=__LIMIT__,EVERY=__EVERY__;
let pos='ALL',timer=null,lastSeq=-1,lastPos='ALL';
const $=s=>document.querySelector(s);
const num=(v,d=1)=>v==null?'-':Number(v).toFixed(d);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function setStatus(t,err){const e=$('#status');e.textContent=t;e.classList.toggle('err',!!err);}
function pickRows(st){return st.rows.filter(r=>pos==='ALL'||r.position===pos).slice(0,LIMIT);}
function draw(st){
 const c=st.clock,rc=st.recompute,r=st.my_roster;
 const until=c.picks_until_my_turn==null?'?':c.picks_until_my_turn;
 $('#clock').innerHTML=
  `<span>pick <b>${c.current_pick}</b>/${st.spec.total_picks}`+(c.round?` · R${c.round}`:'')+`</span>`+
  `<span>on the clock: <b>${c.on_the_clock??'-'}</b></span>`+
  `<span>my slot: <b>${c.my_slot??'?'}</b></span>`+
  (c.complete?`<span class="turn">draft complete</span>`:
   c.my_turn?`<span class="turn">YOU ARE ON THE CLOCK</span>`:
   `<span>until my turn: <b>${until}</b>`+(c.my_next_pick?` (pick ${c.my_next_pick})`:'')+`</span>`);
 if(r){const n=Object.entries(r.needs||{}).sort((a,b)=>b[1]-a[1]);
  const os=r.open_starters||{};
  $('#needs').innerHTML=`<small>my needs</small> `+n.map(([p,v])=>
   `<span class="${os[p]?'hot':''}">${p} ${num(v,2)}${os[p]?' · '+os[p]+' open':''}</span>`).join('')+
   ` <span>bench ${r.open_bench} open</span>`+
   ` <small>· mine: ${(r.picks||[]).map(p=>esc(p.name||p.sleeper_id)+' ('+p.seat+')').join(', ')||'none yet'}</small>`;}
 else $('#needs').innerHTML='<small>my seat unknown — set my_draft_slot or wait for draft_order</small>';
 const b=$('#banner');b.classList.toggle('on',!!rc.error);
 if(rc.error)b.textContent='last recompute failed: '+rc.error+' — showing previous advice';
 const rows=pickRows(st);
 $('#rows').innerHTML=rows.map(x=>{
  const tags=[];
  if(x.cliff)tags.push('<span class="tag cliffTag">CLIFF</span>');
  if(x.run)tags.push(`<span class="tag run">RUN ${x.run_count}</span>`);
  if(x.adp_flag)tags.push(`<span class="tag ${x.adp_flag}">${x.adp_flag}</span>`);
  if(x.disagree)tags.push('<span class="tag disagree">?</span>');
  const s=x.survival;const sc=s==null?'':s<0.35?'lo':s>0.8?'hi':'';
  const cls=[x.cliff?'cliff':'',`t-${x.tier&&x.tier%2?'odd':'even'}`].filter(Boolean).join(' ');
  return `<tr class="${cls}"><td>${x.rank}</td><td class="l">${esc(x.name)}</td><td>${x.position}</td>`+
   `<td class="m">${esc(x.team||'')}</td><td><b>${num(x.pick_score)}</b></td><td>${num(x.vorp)}</td>`+
   `<td><span class="surv ${sc}">${s==null?'n/a':Math.round(s*100)+'%'}</span></td>`+
   `<td class="m">${num(x.adp)}</td><td>${x.tier??'-'}</td><td class="m">${num(x.gap_to_next)}</td>`+
   `<td class="m">${num(x.points)}</td><td class="l">${tags.join('')}</td></tr>`;}).join('');
 setStatus(`recompute #${rc.seq} at ${new Date(rc.computed_at).toLocaleTimeString()} (${rc.elapsed_ms} ms, poll ${st.poller&&st.poller.interval_s?st.poller.interval_s+'s':'?'})`+
  ` · ${st.board.available} available of ${st.board.rows}`+
  (st.running===false?' · poller stopped':'')+(st.poller&&st.poller.status?` · draft ${st.poller.status}`:''),
  st.running===false&&!c.complete);
 $('#start').style.display=st.running===false&&!c.complete?'':'none';
}
async function tick(){
 try{
  const r=await fetch(`/draft/${DID}/state`,{cache:'no-store'});
  if(r.status===404){setStatus('draft not started on the server — press start',true);
   $('#start').style.display='';$('#rows').innerHTML='';return;}
  if(!r.ok){setStatus('HTTP '+r.status,true);return;}
  const st=await r.json();
  if(st.recompute.seq!==lastSeq||pos!==lastPos){lastSeq=st.recompute.seq;lastPos=pos;draw(st);}
 }catch(e){setStatus('fetch failed: '+e,true);}
}
async function start(){
 setStatus('starting (board build takes a few seconds)…');
 try{const r=await fetch(`/draft/${DID}/start`,{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify({season:__SEASON__,interval_s:__INTERVAL__})});
  if(!r.ok)setStatus('start failed: HTTP '+r.status,true);}catch(e){setStatus('start failed: '+e,true);}
 tick();
}
document.querySelectorAll('.filters button[data-pos]').forEach(b=>b.onclick=()=>{
 pos=b.dataset.pos;document.querySelectorAll('.filters button[data-pos]').forEach(x=>x.classList.toggle('on',x===b));tick();});
$('#start').onclick=start;
tick();timer=setInterval(tick,EVERY);
"""

POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


def draft_page(
    draft_id: str, *, season: int, limit: int = 40, refresh_s: float = 2.0, interval_s: float = 2.0
) -> str:
    """The fallback page for one draft. Pure function of its arguments — all live data comes
    from the browser's polling of ``/draft/{id}/state``."""
    did = escape(draft_id)
    js = (
        _JS.replace("__DID__", json.dumps(draft_id))
        .replace("__LIMIT__", str(int(limit)))
        .replace("__EVERY__", str(int(refresh_s * 1000)))
        .replace("__SEASON__", str(int(season)))
        .replace("__INTERVAL__", repr(float(interval_s)))
    )
    buttons = '<button class="on" data-pos="ALL">ALL</button>' + "".join(
        f'<button data-pos="{p}">{p}</button>' for p in POSITIONS
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Lazy Sleeper draft {did}</title><style>{_CSS}</style></head><body>"
        "<header>"
        f"<h1>Lazy Sleeper — live draft <small>{did} · "
        f'<a href="/board.html?season={int(season)}">pre-draft board</a> · '
        f'<a href="/draft/{did}/state">json</a></small></h1>'
        '<div class="clock" id="clock">loading…</div>'
        '<div class="needs" id="needs"></div>'
        f'<div class="filters">{buttons} '
        '<button class="start" id="start" style="display:none">start draft runner</button></div>'
        '<div class="status" id="status">connecting…</div>'
        "</header>"
        '<div class="banner" id="banner"></div>'
        '<table><thead><tr><th>#</th><th class="l">player</th><th>pos</th><th class="m">team</th>'
        '<th>score</th><th>vorp</th><th>surv</th><th class="m">adp</th><th>tier</th>'
        '<th class="m">gap</th><th class="m">pts</th><th class="l">flags</th></tr></thead>'
        '<tbody id="rows"></tbody></table>'
        f"<script>{js}</script></body></html>"
    )


__all__ = ["POSITIONS", "draft_page"]
