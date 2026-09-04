"""Draft-night HTML fallback (LS-37): one self-contained page that polls ``/draft/{id}/state``.

No build step, no external assets, no logic the API doesn't already have: the page is a static
shell whose small script fetches the state document every few seconds and redraws the clock strip,
my needs and the best-available table (``pick_score`` order, tier / cliff / run / survival
columns). Position buttons filter client-side. If the draft isn't running yet the page offers the
**start** button (``POST /draft/{id}/start``), so draft night needs a browser and nothing else.
Readable on a phone or beside the Sleeper room on a second monitor; dark like ``/board.html``.

Redraw rules (LS-63): the clock, status line, banner and start button are redrawn on *every*
successful tick — a stalled or dead poller (``poller.failures_in_a_row``, ``runner_error``,
``running: false``, a DB writer that's behind) must never look like a quiet room. Only the table
is gated, on ``recompute.seq`` going *up*; the gate resets on a 404, on the start button and when
``poller.started_at`` changes (a restart is a fresh engine whose seq starts over). One request is
in flight at a time, so an older response can never overwrite a newer one. Staleness is measured
on the client's clock (time since ``last_ok_at`` last *changed*) — no server/phone skew.

Phone hardening (LS-67): the table lives in its own scrolling ``.wrap`` (the body never scrolls
sideways), ``thead`` is sticky inside it, filter buttons are thumb-sized, and a
``visibilitychange`` handler forces a tick on wake so a locked phone catches up immediately.
"""

# ruff: noqa: E501 — the inline CSS/JS is deliberately one-liner-dense
from __future__ import annotations

import json
from html import escape

_CSS = """
html{height:100%}
body{font:14px/1.35 system-ui,Segoe UI,Roboto,sans-serif;margin:0;background:#111;color:#e6e6e6;
 display:flex;flex-direction:column;height:100vh;height:100dvh}
header{padding:10px 14px;background:#1b1b1b;border-bottom:1px solid #333;flex:none}
h1{font-size:16px;margin:0 0 6px}h1 small,small{color:#999;font-weight:normal}
a{color:#93c5fd}
.clock{display:flex;flex-wrap:wrap;gap:6px 18px;align-items:baseline;margin:4px 0}
.clock b{font-size:18px}.turn{color:#fde047;font-weight:bold}
.cd{font-variant-numeric:tabular-nums;color:#86efac}.cd.warn{color:#fde68a}.cd.hot{color:#f87171;font-weight:bold}
.feed span{display:inline-block;margin:2px 10px 0 0;font-size:12px}.feed small{color:#999}
.needs span{display:inline-block;margin:2px 6px 0 0;padding:0 6px;border-radius:3px;background:#222;
 border:1px solid #444;font-size:12px}.needs span.hot{border-color:#f59e0b;color:#fde68a}
.filters button{margin:4px 4px 0 0;padding:6px 12px;min-height:36px;min-width:44px;border:1px solid #444;
 background:#222;color:#ddd;border-radius:4px;cursor:pointer;font-size:14px}
.filters button.on{background:#3b82f6;color:#fff}
.filters button.start{background:#16a34a;color:#fff;border-color:#16a34a}
.filters button.stop{background:#7f1d1d;color:#fff;border-color:#7f1d1d}
.status{font-size:12px;color:#999;margin-top:4px}.status.err{color:#fca5a5}.status.warn{color:#fde68a}
.banner{background:#7f1d1d;color:#fecaca;padding:6px 14px;display:none;flex:none}.banner.on{display:block}
.wrap{flex:1 1 auto;min-height:0;overflow:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%}th,td{padding:4px 8px;text-align:right;
 border-bottom:1px solid #2a2a2a;white-space:nowrap}th{background:#1b1b1b}
thead th{position:sticky;top:0;z-index:1}
td.l,th.l{text-align:left}tr.cliff td{border-bottom:2px solid #ef4444}
.tag{display:inline-block;padding:0 6px;border-radius:3px;font-size:12px;margin-left:4px}
.value{background:#14532d;color:#bbf7d0}.reach{background:#7f1d1d;color:#fecaca}
.run{background:#7c2d12;color:#fed7aa}.cliffTag{background:#ef4444;color:#fff}
.disagree{background:#713f12;color:#fde68a}
tr.t-odd td{background:#161616}tr.mine td{background:#1e3a8a}
.inj{color:#f87171;font-size:11px;margin-left:4px}
.surv{display:inline-block;min-width:34px}.surv.lo{color:#f87171}.surv.hi{color:#86efac}
@media (max-width:640px){th.m,td.m{display:none}body{font-size:13px}th,td{padding:3px 5px}}
"""

_JS = """
const DID=__DID__,LIMIT=__LIMIT__,EVERY=__EVERY__;
let pos='ALL',timer=null,lastSeq=-1,lastPos='ALL',lastRun=null,inflight=false,okSeen=null,okAt=0;
const $=s=>document.querySelector(s);
const num=(v,d=1)=>v==null?'-':Number(v).toFixed(d);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function setStatus(t,cls){const e=$('#status');e.textContent=t;e.className='status'+(cls?' '+cls:'');}
function setBanner(t){const b=$('#banner');b.classList.toggle('on',!!t);b.textContent=t||'';}
function resetView(){lastSeq=-1;lastRun=null;}
function pickRows(st){return st.rows.filter(r=>pos==='ALL'||r.position===pos).slice(0,LIMIT);}
let deadline=null;   // LS-56: server-derived pick deadline; ticked locally, independent of seq
function drawCountdown(){
 const e=$('#cd');if(!e)return;
 if(deadline==null){e.textContent='';e.className='';return;}
 const s=Math.max(0,Math.round((deadline-Date.now())/1000));
 e.textContent=`⏱ ${s}s`;e.className=s<=10?'cd hot':s<=20?'cd warn':'cd';
}
function drawClock(st){
 const c=st.clock,r=st.my_roster;
 const until=c.picks_until_my_turn==null?'?':c.picks_until_my_turn;
 deadline=c.pick_deadline?Date.parse(c.pick_deadline):null;
 $('#clock').innerHTML=
  `<span>pick <b>${c.current_pick}</b>/${st.spec.total_picks}`+(c.round?` · R${c.round}`:'')+`</span>`+
  `<span>on the clock: <b>${c.on_the_clock??'-'}</b>`+(c.on_the_clock_team_name?` ${esc(c.on_the_clock_team_name)}`:'')+` <span id="cd"></span></span>`+
  `<span>my slot: <b>${c.my_slot??'?'}</b></span>`+
  (c.complete?`<span class="turn">draft complete</span>`:
   c.my_turn?`<span class="turn">YOU ARE ON THE CLOCK</span>`:
   `<span>until my turn: <b>${until}</b>`+(c.my_next_pick?` (pick ${c.my_next_pick})`:'')+`</span>`);
 drawCountdown();
 const feed=st.recent_picks||[];
 $('#feed').innerHTML=feed.length?`<small>last picks</small> `+feed.map(p=>
  `<span>${p.pick_no}. ${esc(p.name||p.sleeper_id)} <small>${p.position||''}${p.team_name?' · '+esc(p.team_name):p.slot?' · slot '+p.slot:''}</small></span>`).join(''):'';
 if(r){const n=Object.entries(r.needs||{}).sort((a,b)=>b[1]-a[1]);
  const os=r.open_starters||{};
  $('#needs').innerHTML=`<small>my needs</small> `+n.map(([p,v])=>
   `<span class="${os[p]?'hot':''}">${p} ${num(v,2)}${os[p]?' · '+os[p]+' open':''}</span>`).join('')+
   ` <span>bench ${r.open_bench} open</span>`+
   ` <small>· mine: ${(r.picks||[]).map(p=>esc(p.name||p.sleeper_id)+' ('+p.seat+')').join(', ')||'none yet'}</small>`;}
 else $('#needs').innerHTML='<small>my seat unknown — set my_draft_slot or wait for draft_order</small>';
}
function drawHealth(st){
 // every tick, whatever seq did: a quiet room and a dead poller must not look alike (LS-63)
 const p=st.poller||{},w=p.persist||{},rc=st.recompute,c=st.clock,now=Date.now();
 if(p.last_ok_at&&p.last_ok_at!==okSeen){okSeen=p.last_ok_at;okAt=now;}
 const age=okSeen?Math.round((now-okAt)/1000):null;   // client clock only: no skew with the server
 const idle=p.mode==='idle';
 const iv=(idle?p.idle_poll_s:p.interval_s)||EVERY/1000;   // LS-77: idle re-reads the doc slowly
 const parts=[],cls=[];
 parts.push(rc.seq?`recompute #${rc.seq} at ${new Date(rc.computed_at).toLocaleTimeString()} (${rc.elapsed_ms} ms)`:'board loaded, no recompute yet');
 if(idle){const st0=p.start_time?new Date(p.start_time):null,wake=p.idle_until?new Date(p.idle_until):null;
  parts.push(`idle until ${wake?wake.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}):'?'}`+(st0?` (draft ${st0.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})})`:'')+` · doc every ${iv}s`+(age==null?'':` · last ok ${age}s ago`));}
 else parts.push(`poll ${iv}s`+(age==null?'':` · last ok ${age}s ago`));
 parts.push(`${st.board.available} available of ${st.board.rows}`);
 if(p.status)parts.push(`draft ${p.status}`);
 if(p.failures_in_a_row>0){parts.push(`poll failing ×${p.failures_in_a_row}: ${p.last_error||'?'}`);cls.push('err');}
 else if(age!=null&&age>Math.max(3*iv,10)&&st.running!==false){parts.push('poll stale');cls.push('err');}
 if(w.failures_in_a_row>0){parts.push(`DB sync behind (${w.pending} pending: ${w.last_error||'?'}) — advice unaffected`);cls.push('warn');}
 if(p.degraded)parts.push('started without DB (picks re-emitted)');
 if(st.running===false&&!c.complete){parts.push('poller stopped');cls.push('err');}
 setStatus(parts.join(' · '),cls.includes('err')?'err':cls.includes('warn')?'warn':'');
 setBanner(p.runner_error?`draft runner died: ${p.runner_error} — press start`:
  rc.error?`last recompute failed: ${rc.error} — showing previous advice`:
  p.failures_in_a_row>=3?`Sleeper unreachable (${p.failures_in_a_row} polls: ${p.last_error||'?'}) — advice frozen until it answers`:'');
 $('#start').style.display=st.running===false&&!c.complete?'':'none';
 $('#stop').style.display=st.running!==false&&!c.complete?'':'none';
}
function drawRows(st){
 const rows=pickRows(st);
 $('#rows').innerHTML=rows.map(x=>{
  const tags=[];
  if(x.cliff)tags.push('<span class="tag cliffTag">CLIFF</span>');
  if(x.run)tags.push(`<span class="tag run">RUN ${x.run_count}</span>`);
  if(x.adp_flag)tags.push(`<span class="tag ${x.adp_flag}">${x.adp_flag}</span>`);
  if(x.disagree)tags.push('<span class="tag disagree">?</span>');
  const s=x.survival;const sc=s==null?'':s<0.35?'lo':s>0.8?'hi':'';
  const cls=[x.cliff?'cliff':'',`t-${x.tier&&x.tier%2?'odd':'even'}`].filter(Boolean).join(' ');
  const inj=x.injury_status?`<span class="inj">${esc(x.injury_status)}</span>`:'';
  return `<tr class="${cls}"><td>${x.rank}</td><td class="l">${esc(x.name)}${inj}</td><td>${x.position}</td>`+
   `<td class="m">${esc(x.team||'')}</td><td class="m">${x.bye??'-'}</td><td><b>${num(x.pick_score)}</b></td><td>${num(x.vorp)}</td>`+
   `<td><span class="surv ${sc}">${s==null?'n/a':Math.round(s*100)+'%'}</span></td>`+
   `<td class="m">${num(x.adp)}</td><td>${x.tier??'-'}</td><td class="m">${num(x.gap_to_next)}</td>`+
   `<td class="m">${num(x.points)}</td><td class="l">${tags.join('')}</td></tr>`;}).join('');
}
async function tick(){
 if(inflight)return;inflight=true;   // one request at a time: no out-of-order redraws
 try{
  const r=await fetch(`/draft/${DID}/state`,{cache:'no-store'});
  if(r.status===404){resetView();setStatus('draft not started on the server — press start','err');
   setBanner('');$('#start').style.display='';$('#rows').innerHTML='';return;}
  if(!r.ok){setStatus('HTTP '+r.status,'err');return;}
  const st=await r.json();
  const run=(st.poller&&st.poller.started_at)||null;
  if(run!==lastRun){lastRun=run;lastSeq=-1;}   // a restarted runner is a fresh engine: redraw all
  drawClock(st);drawHealth(st);
  if(st.recompute.seq>lastSeq||pos!==lastPos){lastSeq=st.recompute.seq;lastPos=pos;drawRows(st);}
 }catch(e){setStatus('fetch failed: '+e,'err');}
 finally{inflight=false;}
}
async function start(){
 resetView();setStatus('starting (board build takes a few seconds)…');
 try{const r=await fetch(`/draft/${DID}/start`,{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify({season:__SEASON__,interval_s:__INTERVAL__})});
  if(!r.ok)setStatus('start failed: HTTP '+r.status,'err');}catch(e){setStatus('start failed: '+e,'err');}
 tick();
}
async function stop(){
 setStatus('stopping the draft runner…');
 try{const r=await fetch(`/draft/${DID}/stop`,{method:'POST'});
  if(!r.ok)setStatus('stop failed: HTTP '+r.status,'err');}catch(e){setStatus('stop failed: '+e,'err');}
 tick();
}
document.querySelectorAll('.filters button[data-pos]').forEach(b=>b.onclick=()=>{
 pos=b.dataset.pos;document.querySelectorAll('.filters button[data-pos]').forEach(x=>x.classList.toggle('on',x===b));tick();});
$('#start').onclick=start;$('#stop').onclick=stop;
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')tick();});
tick();timer=setInterval(tick,EVERY);setInterval(drawCountdown,1000);
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
        f'<a href="/draft/{did}/state">json</a> · '
        f'<a href="/board/config.html?draft_id={did}">tuning</a></small></h1>'
        '<div class="clock" id="clock">loading…</div>'
        '<div class="needs" id="needs"></div>'
        '<div class="feed" id="feed"></div>'
        f'<div class="filters">{buttons} '
        '<button class="start" id="start" style="display:none">start draft runner</button>'
        '<button class="stop" id="stop" style="display:none">stop draft runner</button></div>'
        '<div class="status" id="status">connecting…</div>'
        "</header>"
        '<div class="banner" id="banner"></div>'
        '<div class="wrap">'
        '<table><thead><tr><th>#</th><th class="l">player</th><th>pos</th><th class="m">team</th>'
        '<th class="m">bye</th>'
        '<th>score</th><th>vorp</th><th>surv</th><th class="m">adp</th><th>tier</th>'
        '<th class="m">gap</th><th class="m">pts</th><th class="l">flags</th></tr></thead>'
        '<tbody id="rows"></tbody></table></div>'
        f"<script>{js}</script></body></html>"
    )


# --- tuning page ---------------------------------------------------------------------------------

# (field, label, help) — order is the page order; types come from the live /board/config values
DIALS: tuple[tuple[str, str, str], ...] = (
    ("need_bonus", "need bonus", "pick_score pts per unit of my positional need (LS-33)"),
    ("survival_sigma_min", "survival σ floor", "picks of ADP scatter, minimum"),
    ("survival_sigma_pct", "survival σ %", "…or this fraction of ADP, whichever is larger"),
    ("demand_shift", "demand shift", "window stretch per unit of relative positional demand"),
    ("run_window", "run window", "picks looked back for a position run"),
    ("run_threshold", "run threshold", "run when this many in the window share a position"),
    ("run_streak", "run streak", "…or this many consecutive most-recent picks do"),
    ("late_rounds", "late rounds", "K/DEF need bonus only in the last N rounds (0 = always)"),
    ("cliff_gap", "cliff gap *", "season pts drop to the next player → CLIFF"),
    ("gap_multiplier", "tier gap ×*", "tier break at this multiple of the position's median gap"),
    ("min_gap", "tier min gap *", "…but never on a drop smaller than this"),
    ("adp_min_delta", "ADP Δ floor *", "|ADP − rank| floor for a value/reach flag"),
    ("adp_pct", "ADP Δ % *", "…or this fraction of ADP, whichever is larger"),
    ("disagree_min_pts", "disagree pts *", "provider spread floor for a disagreement flag"),
    ("disagree_pct", "disagree % *", "…or this fraction of blended points"),
    ("debias_disagreement", "debias *", "remove each provider's position bias before comparing"),
    ("stream_depth", "stream depth *", "K/DEF replacement rank (0 = last starter)"),
)
LIVE_DIALS = frozenset(d[0] for d in DIALS if not d[1].endswith("*"))

_CFG_CSS = (
    _CSS
    + """
main{padding:12px 14px;max-width:720px}form{display:grid;grid-template-columns:1fr auto;gap:6px 12px;
 align-items:center}label{display:flex;flex-direction:column}label small{font-size:11px}
input[type=number]{width:7em;padding:4px;background:#222;color:#eee;border:1px solid #444;border-radius:4px}
.actions{margin:12px 0;display:flex;flex-wrap:wrap;gap:8px}.actions button{padding:6px 12px}
.note{font-size:12px;color:#999}.ok{color:#86efac}
"""
)

_CFG_JS = """
const DID=__DID__,LIVE=new Set(__LIVE__);
const $=s=>document.querySelector(s);
function setStatus(t,err){const e=$('#status');e.textContent=t;e.classList.toggle('err',!!err);e.classList.toggle('ok',!err);}
async function load(){
 const r=await fetch('/board/config',{cache:'no-store'});if(!r.ok){setStatus('load failed: HTTP '+r.status,true);return;}
 const c=await r.json();
 for(const [k,v] of Object.entries(c)){const el=document.querySelector(`[name="${k}"]`);if(!el)continue;
  if(el.type==='checkbox')el.checked=!!v;else el.value=v;}
 setStatus('loaded · updated '+(c.updated_at||'?'));
}
function body(){const b={};for(const el of document.querySelectorAll('[name]')){
 if(el.type==='checkbox')b[el.name]=el.checked;else if(el.value!=='')b[el.name]=Number(el.value);}return b;}
async function save(){
 const r=await fetch('/board/config',{method:'PUT',headers:{'content-type':'application/json'},body:JSON.stringify(body())});
 if(!r.ok){setStatus('save failed: HTTP '+r.status+' '+(await r.text()).slice(0,200),true);return false;}
 setStatus('saved');return true;
}
async function apply(restart){
 if(!await save())return;
 const r=await fetch(`/draft/${DID}/config?restart=${restart}`,{method:'POST'});
 if(r.status===404){setStatus('saved; draft '+DID+' is not running (start it from the draft page)',true);return;}
 if(!r.ok){setStatus('apply failed: HTTP '+r.status,true);return;}
 const j=await r.json();setStatus((restart?'restarted, board rebuilt':'applied live')+' · recompute #'+j.recompute_seq+(j.error?' · ERROR '+j.error:''),!!j.error);
}
$('#save').onclick=save;$('#apply').onclick=()=>apply(false);$('#restart').onclick=()=>apply(true);
$('#reload').onclick=load;load();
"""


def config_page(draft_id: str) -> str:
    """The tuning page: one form over ``board_config``. Dials marked * are baked into the board
    at build time and need the **restart** button; the rest apply to the running draft instantly."""
    did = escape(draft_id)
    rows = []
    for name, label, help_ in DIALS:
        if name == "debias_disagreement":
            inp = f'<input type="checkbox" name="{name}">'
        elif name in ("run_window", "run_threshold", "run_streak", "late_rounds", "stream_depth"):
            inp = f'<input type="number" name="{name}" step="1" min="0">'
        else:
            inp = f'<input type="number" name="{name}" step="any" min="0">'
        rows.append(f"<label>{escape(label)}<small>{escape(help_)}</small></label>{inp}")
    js = _CFG_JS.replace("__DID__", json.dumps(draft_id)).replace(
        "__LIVE__", json.dumps(sorted(LIVE_DIALS))
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Lazy Sleeper tuning</title><style>{_CFG_CSS}</style></head><body>"
        f"<header><h1>Lazy Sleeper — tuning <small>draft {did} · "
        f'<a href="/draft/{did}/state.html">draft page</a> · <a href="/board/config">json</a>'
        "</small></h1>"
        '<div class="status" id="status">loading…</div></header>'
        f'<main><form onsubmit="return false">{"".join(rows)}</form>'
        '<div class="actions">'
        '<button id="save">save</button>'
        '<button id="apply" class="start">save + apply to live draft</button>'
        '<button id="restart">save + restart draft runner (rebuild board)</button>'
        '<button id="reload">reload</button></div>'
        '<p class="note">Unstarred dials change survival / runs / need bonus and take effect on the '
        "next recompute. Starred (*) dials are baked into the board rows (tiers, cliffs, ADP and "
        "disagreement flags, K/DEF stream depth) — use <b>restart</b>: it rebuilds the board (a few "
        "seconds) and restores the draft state from the database. Settings persist in "
        "<code>derived.board_config</code> and also steer <code>lazy board regen</code>.</p></main>"
        f"<script>{js}</script></body></html>"
    )


__all__ = ["DIALS", "LIVE_DIALS", "POSITIONS", "config_page", "draft_page"]
