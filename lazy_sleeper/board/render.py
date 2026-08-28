"""CSV and single-file HTML renderings of a persisted board (LS-30).

The HTML page is the draft-night fallback: no external assets, no framework, works from a file
or from ``GET /board.html``. Position buttons filter client-side; flags are colour-coded.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from html import escape
from typing import Any

CSV_COLUMNS = (
    "rank",
    "name",
    "position",
    "team",
    "injury_status",
    "bye",
    "points",
    "baseline",
    "vorp",
    "pos_rank",
    "tier",
    "cliff",
    "gap_to_next",
    "adp",
    "adp_delta",
    "adp_flag",
    "spread",
    "disagree",
    "sleeper_id",
)


def to_csv(rows: Sequence[Mapping[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: _csv_value(r.get(k)) for k in CSV_COLUMNS})
    return buf.getvalue()


def _csv_value(v: Any) -> Any:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        return f"{v:.2f}"
    return "" if v is None else v


_CSS = """
body{font:14px/1.35 system-ui,Segoe UI,Roboto,sans-serif;margin:0;background:#111;color:#e6e6e6}
header{padding:12px 16px;background:#1b1b1b;position:sticky;top:0;border-bottom:1px solid #333}
h1{font-size:16px;margin:0 0 6px}small{color:#999}
.filters button{margin:4px 4px 0 0;padding:4px 10px;border:1px solid #444;background:#222;
 color:#ddd;border-radius:4px;cursor:pointer}.filters button.on{background:#3b82f6;color:#fff}
table{border-collapse:collapse;width:100%}th,td{padding:4px 8px;text-align:right;
 border-bottom:1px solid #2a2a2a;white-space:nowrap}th{position:sticky;top:74px;background:#1b1b1b}
td.l,th.l{text-align:left}tr.cliff td{border-bottom:2px solid #ef4444}
.tag{display:inline-block;padding:0 6px;border-radius:3px;font-size:12px;margin-left:4px}
.value{background:#14532d;color:#bbf7d0}.reach{background:#7f1d1d;color:#fecaca}
.disagree{background:#713f12;color:#fde68a}.cliffTag{background:#ef4444;color:#fff}
.inj{color:#f87171;font-size:12px}.hidden{display:none}tr.t-odd td{background:#161616}
"""

_JS = """
const btns=[...document.querySelectorAll('.filters button')];
btns.forEach(b=>b.onclick=()=>{btns.forEach(x=>x.classList.toggle('on',x===b));
 const p=b.dataset.pos;document.querySelectorAll('tbody tr').forEach(tr=>
 tr.classList.toggle('hidden',p!=='ALL'&&tr.dataset.pos!==p));});
"""


def to_html(meta: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> str:
    positions = sorted({r["position"] for r in rows})
    gen = meta.get("generated_at")
    gen_s = gen.strftime("%Y-%m-%d %H:%M UTC") if hasattr(gen, "strftime") else str(gen or "")
    head = (
        f"<h1>Lazy Sleeper draft board — {escape(str(meta.get('season', '')))} "
        f"<small>{escape(str(meta.get('provider', '')))} · {escape(str(meta.get('baseline', '')))} "
        f"baseline · generated {escape(gen_s)} · {len(rows)} players</small></h1>"
        '<div class="filters"><button class="on" data-pos="ALL">ALL</button>'
        + "".join(f'<button data-pos="{escape(p)}">{escape(p)}</button>' for p in positions)
        + "</div>"
    )
    cols = (
        '<tr><th>#</th><th class="l">player</th><th>pos</th><th>team</th><th>bye</th><th>pts</th>'
        "<th>vorp</th>"
        "<th>pos#</th><th>tier</th><th>gap</th><th>adp</th><th>Δadp</th><th>spread</th>"
        '<th class="l">flags</th></tr>'
    )
    body = "".join(_row_html(r) for r in rows)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Lazy Sleeper board {escape(str(meta.get('season', '')))}</title>"
        f"<style>{_CSS}</style></head><body><header>{head}</header>"
        f"<table><thead>{cols}</thead><tbody>{body}</tbody></table>"
        f"<script>{_JS}</script></body></html>"
    )


def _num(v: Any, fmt: str = ".1f") -> str:
    return "-" if v is None else format(v, fmt)


def _row_html(r: Mapping[str, Any]) -> str:
    tags = []
    if r.get("cliff"):
        tags.append('<span class="tag cliffTag">CLIFF</span>')
    if r.get("adp_flag"):
        f = escape(str(r["adp_flag"]))
        tags.append(f'<span class="tag {f}">{f}</span>')
    if r.get("disagree"):
        tags.append('<span class="tag disagree">DISAGREE</span>')
    injury = (
        f' <span class="inj">{escape(str(r["injury_status"]))}</span>'
        if r.get("injury_status")
        else ""
    )
    tier = r.get("tier")
    classes = ["cliff" if r.get("cliff") else "", f"t-{'odd' if tier and tier % 2 else 'even'}"]
    return (
        f'<tr class="{" ".join(c for c in classes if c)}" data-pos="{escape(r["position"])}">'
        f'<td>{r["rank"]}</td><td class="l">{escape(str(r["name"]))}{injury}</td>'
        f"<td>{escape(r['position'])}</td><td>{escape(str(r.get('team') or ''))}</td>"
        f"<td>{'-' if r.get('bye') is None else r['bye']}</td>"
        f"<td>{_num(r['points'])}</td><td>{_num(r['vorp'])}</td><td>{r['pos_rank']}</td>"
        f"<td>{'-' if tier is None else tier}</td><td>{_num(r.get('gap_to_next'))}</td>"
        f"<td>{_num(r.get('adp'))}</td><td>{_num(r.get('adp_delta'), '+.0f')}</td>"
        f'<td>{_num(r.get("spread"))}</td><td class="l">{"".join(tags)}</td></tr>'
    )
