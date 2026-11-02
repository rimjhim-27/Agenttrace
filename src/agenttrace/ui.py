"""A deliberately tiny, dependency-free viewer served at ``/``.

All trace data is rendered with ``textContent`` (never ``innerHTML``): traces contain
arbitrary strings produced by LLMs and users, so treating them as HTML would be an XSS hole.
A richer Next.js dashboard is on the roadmap (docs/ROADMAP.md).
"""

INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgentTrace</title>
<style>
:root{--bg:#0f1115;--fg:#e6e8ee;--mut:#8b93a1;--card:#171a21;--line:#262b36;--ok:#4cc38a;--err:#ef5b5b;
--agent:#c792ea;--llm:#7aa2ff;--tool:#f0b35a;--custom:#8b93a1}
@media (prefers-color-scheme:light){:root{--bg:#f6f7f9;--fg:#15171c;--mut:#5b6472;--card:#fff;--line:#dde1e8}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:baseline}
h1{font-size:18px;margin:0}small,.mut{color:var(--mut)}
main{display:grid;grid-template-columns:minmax(300px,380px) 1fr;gap:16px;padding:16px 20px}
@media (max-width:800px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.run{padding:8px;border-radius:8px;cursor:pointer;border:1px solid transparent}
.run:hover,.run.sel{border-color:var(--line);background:var(--bg)}
.pill{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line)}
.ok{color:var(--ok)}.error{color:var(--err)}.running{color:var(--tool)}
.span{margin:4px 0;padding:6px 8px;border-left:3px solid var(--custom);background:var(--bg);border-radius:0 6px 6px 0}
.span.agent{border-color:var(--agent)}.span.llm{border-color:var(--llm)}.span.tool{border-color:var(--tool)}
.span.err{outline:1px solid var(--err)}
.bar{height:4px;background:var(--line);border-radius:2px;margin-top:4px}.bar i{display:block;height:4px;border-radius:2px;background:var(--llm)}
pre{margin:6px 0 0;white-space:pre-wrap;word-break:break-word;color:var(--mut);font-size:12px;max-height:160px;overflow:auto}
.ins{padding:6px 8px;margin:4px 0;border-radius:6px;background:var(--bg);border-left:3px solid var(--tool)}
.ins.error{border-color:var(--err)}.ins.info{border-color:var(--llm)}
</style></head><body>
<header><h1>AgentTrace</h1><small id="meta"></small></header>
<main><section class="card" id="runs"><b>Runs</b><div id="runlist"></div></section>
<section class="card" id="detail"><span class="mut">Select a run.</span></section></main>
<script>
const KEY = new URLSearchParams(location.search).get('key');
const H = KEY ? {'X-API-Key': KEY} : {};
const api = p => fetch('/api/v1' + p, {headers: H}).then(r => { if(!r.ok) throw new Error(r.status + ' ' + p); return r.json(); });
function el(tag, cls, text){ const e = document.createElement(tag); if(cls) e.className = cls; if(text !== undefined) e.textContent = text; return e; }
const ms = v => v == null ? '-' : (v >= 1000 ? (v/1000).toFixed(2) + 's' : v.toFixed(1) + 'ms');
let selected = null;
async function loadRuns(){
  const data = await api('/runs?limit=100'); const box = document.getElementById('runlist'); box.replaceChildren();
  document.getElementById('meta').textContent = data.total + ' runs';
  data.runs.forEach(r => {
    const d = el('div', 'run' + (r.trace_id === selected ? ' sel' : ''));
    const top = el('div'); top.append(el('b', '', r.name + ' '), el('span', 'pill ' + r.status, r.status));
    if(r.replay_of) top.append(' ', el('span', 'pill', 'replay'));
    d.append(top, el('div', 'mut', r.trace_id.slice(0, 8) + ' | ' + r.span_count + ' spans | ' + ms(r.duration_ms) +
      ' | ' + (r.tokens_in + r.tokens_out) + ' tok' + (r.error_count ? ' | ' + r.error_count + ' err' : '')));
    d.onclick = () => { selected = r.trace_id; loadRuns(); showRun(r.trace_id); };
    box.append(d);
  });
}
async function showRun(id){
  const [run, ins] = await Promise.all([api('/runs/' + id), api('/runs/' + id + '/insights')]);
  const box = document.getElementById('detail'); box.replaceChildren();
  box.append(el('b', '', run.run.name), el('div', 'mut', run.run.trace_id));
  if(ins.insights.length){ box.append(el('h4', '', 'Insights')); ins.insights.forEach(i => box.append(el('div', 'ins ' + i.severity, i.message))); }
  box.append(el('h4', '', 'Spans'));
  const spans = run.spans, ids = new Set(spans.map(s => s.span_id)), kids = {};
  spans.forEach(s => { const p = ids.has(s.parent_id) ? s.parent_id : ''; (kids[p] = kids[p] || []).push(s); });
  const total = Math.max(1, ...spans.map(s => s.duration_ms));
  const draw = (s, depth) => {
    const d = el('div', 'span ' + s.kind + (s.error ? ' err' : '')); d.style.marginLeft = (depth * 18) + 'px';
    d.append(el('b', '', s.name), el('span', 'mut', '  [' + s.kind + '] ' + ms(s.duration_ms) +
      (s.attrs.tokens_in != null ? '  ' + s.attrs.tokens_in + '/' + s.attrs.tokens_out + ' tok' : '') + (s.attrs.replayed ? '  (replayed)' : '')));
    if(s.error) d.append(el('div', 'error', s.error));
    const bar = el('div', 'bar'), fill = el('i'); fill.style.width = Math.max(1, 100 * s.duration_ms / total) + '%'; bar.append(fill); d.append(bar);
    const io = el('details'); io.append(el('summary', 'mut', 'input / output'),
      el('pre', '', 'in:  ' + JSON.stringify(s.input) + '\\nout: ' + JSON.stringify(s.output))); d.append(io);
    box.append(d); (kids[s.span_id] || []).forEach(c => draw(c, depth + 1));
  };
  (kids[''] || []).forEach(s => draw(s, 0));
}
loadRuns().catch(e => { document.getElementById('runlist').textContent = String(e); });
</script></body></html>
"""
