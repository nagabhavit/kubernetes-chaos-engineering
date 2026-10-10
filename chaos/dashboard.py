#!/usr/bin/env python3
"""
Live chaos dashboard (read-only).

    python chaos/dashboard.py          ->  open http://127.0.0.1:8090

Reads the files the controller writes (results/runs/<run_id>/events.jsonl,
probes.csv, summary.json) and polls `kubectl get deployments` for cluster
health. It cannot start, stop or change anything. Standard library only.
"""
import csv
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "results" / "runs"
KILL_FILE = REPO / "chaos" / "KILL"
STATE_FILE = REPO / "chaos" / "state" / "active_fault.json"
CONTEXT, NAMESPACE = "kind-boutique-chaos", "boutique"
HOST, PORT = "127.0.0.1", 8090

_cluster = {"ts": 0, "data": None}
_lock = threading.Lock()


def cluster():
    with _lock:
        if time.time() - _cluster["ts"] < 2.5:
            return _cluster["data"]
    try:
        r = subprocess.run(["kubectl", "--context", CONTEXT, "-n", NAMESPACE, "get", "deployments", "-o", "json"],
                           capture_output=True, text=True, timeout=8)
        if r.returncode != 0:
            data = {"error": r.stderr.strip()[:300]}
        else:
            data = {"deployments": [{
                "name": d["metadata"]["name"],
                "want": d["spec"].get("replicas", 1) or 0,
                "ready": d.get("status", {}).get("readyReplicas", 0) or 0,
            } for d in json.loads(r.stdout)["items"]]}
    except Exception as e:  # noqa: BLE001
        data = {"error": repr(e)}
    with _lock:
        _cluster.update(ts=time.time(), data=data)
    return data


def read_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def read_events(run_dir):
    out = []
    p = run_dir / "events.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # partially written line
    return out


def read_probes(run_dir, limit=2400):
    p = run_dir / "probes.csv"
    if not p.exists():
        return []
    rows = []
    with p.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append([float(r["unix_ts"]), int(r["ok"]), float(r["latency_ms"]), r["path"], r["status"], r["kind"]])
            except (ValueError, KeyError, TypeError):
                pass
    return rows[-limit:]


def list_runs():
    if not RUNS.exists():
        return []
    runs = []
    for d in sorted((x for x in RUNS.iterdir() if x.is_dir()), reverse=True):
        s = read_json(d / "summary.json")
        ev = read_events(d) if not s else []
        runs.append({
            "id": d.name,
            "finished": bool(s),
            "status": s["status"] if s else (ev[-1]["event"] if ev else "starting"),
            "target": (s or {}).get("request", {}).get("target") or (ev[0].get("target") if ev else None),
            "fault_duration_s": (s or {}).get("fault_duration_s"),
            "fault_window": (s or {}).get("fault_window"),
            "recovery": (s or {}).get("recovery"),
        })
    return runs


def run_detail(run_id):
    valid = {x.name for x in RUNS.iterdir() if x.is_dir()} if RUNS.exists() else set()
    if run_id not in valid:  # no path traversal: only known run folders
        return None
    d = RUNS / run_id
    return {"id": run_id, "summary": read_json(d / "summary.json"), "events": read_events(d), "probes": read_probes(d)}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, PAGE, "text/html")
        if path == "/api/overview":
            return self._send(200, json.dumps({
                "runs": list_runs(), "cluster": cluster(), "kill_armed": KILL_FILE.exists(),
                "active_fault": read_json(STATE_FILE) if STATE_FILE.exists() else None, "now": time.time()}))
        if path.startswith("/api/run/"):
            det = run_detail(path.rsplit("/", 1)[-1])
            return self._send(200, json.dumps(det)) if det else self._send(404, '{"error":"unknown run"}')
        self._send(404, '{"error":"not found"}')

    def log_message(self, *a):
        pass


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chaos Control Dashboard</title>
<style>
:root{--bg:#0f1216;--panel:#171b21;--line:#262c35;--text:#e6e9ee;--mute:#8b94a3;--ok:#3fb68b;--bad:#ef4f6b;--warn:#f0a43a;--info:#4c9aff;--grid:#222831}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
body{overflow-x:hidden}header{display:flex;align-items:center;gap:14px;padding:14px 20px;border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.2px}.sub{color:var(--mute);font-size:12px}
.pill{padding:3px 10px;border-radius:99px;font-size:12px;font-weight:600;border:1px solid currentColor}
.p-ok{color:var(--ok)}.p-bad{color:var(--bad)}.p-warn{color:var(--warn)}.p-info{color:var(--info)}.p-mute{color:var(--mute)}
.spacer{flex:1}main{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;padding:16px 20px}
@media(max-width:1000px){main{grid-template-columns:minmax(0,1fr)}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.panel h2{font-size:12px;text-transform:uppercase;letter-spacing:.8px;color:var(--mute);margin:0 0 10px;font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}
@media(max-width:700px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.kpi .v{font-size:20px}}
.kpi .v{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums}.kpi .l{color:var(--mute);font-size:12px}
canvas{width:100%;display:block}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--mute);font-size:12px;margin-top:8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:6px 6px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--mute);font-weight:500;font-size:12px}tr.sel td{background:#1f2630}tr.click{cursor:pointer}tr.click:hover td{background:#1c222a}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.ev{max-height:300px;overflow:auto;overflow-wrap:anywhere}.ev div{padding:3px 0;border-bottom:1px dashed var(--line)}.ev b{font-weight:600}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.stack>*+*{margin-top:16px}.banner{padding:10px 14px;border-radius:8px;margin:0 20px;font-weight:600}
.b-bad{background:#3a1820;color:#ffb3c0}.b-warn{background:#3a2a12;color:#ffd08a}
</style></head><body>
<header>
  <h1>AI-Driven Chaos Engineering &middot; Live Dashboard</h1>
  <span class="sub">kind-boutique-chaos / boutique &middot; read-only</span>
  <span class="spacer"></span>
  <span id="runPill" class="pill p-mute">—</span>
  <span id="clusterPill" class="pill p-mute">cluster …</span>
</header>
<div id="banners"></div>
<main>
  <div class="stack">
    <div class="kpis">
      <div class="panel kpi"><div class="v" id="kErr">—</div><div class="l" id="kErrSub">Error rate (last 5 s)</div></div>
      <div class="panel kpi"><div class="v" id="kP95">—</div><div class="l" id="kP95Sub">P95 latency (last 5 s)</div></div>
      <div class="panel kpi"><div class="v" id="kFault">—</div><div class="l">Fault duration</div></div>
      <div class="panel kpi"><div class="v" id="kRec">—</div><div class="l">User recovery after restore</div><div class="l" id="kRecSub" style="font-size:11px"></div></div>
    </div>
    <div class="panel">
      <h2 id="chartTitle">Probe timeline</h2>
      <canvas id="lat" height="230"></canvas>
      <canvas id="err" height="140" style="margin-top:6px"></canvas>
      <div class="legend"><span><i style="background:var(--info)"></i>OK request</span><span><i style="background:var(--bad)"></i>Failed (5xx / timeout)</span>
        <span><i style="background:var(--bad);height:2px"></i>Error rate, 5 s window</span><span><i style="background:var(--mute);height:2px"></i>Abort threshold</span>
        <span><i style="background:var(--warn);height:2px"></i>Fault</span><span><i style="background:var(--ok);height:2px"></i>Restore</span><span><i style="background:var(--mute);height:2px"></i>K8s ready</span><span><i style="background:var(--info);height:2px"></i>Users recovered</span></div>
    </div>
    <div class="panel"><h2>Runs</h2><div style="overflow-x:auto"><table id="runs"></table></div></div>
  </div>
  <div class="stack">
    <div class="panel"><h2>Cluster</h2><table id="deps"></table></div>
    <div class="panel"><h2>Run events</h2><div class="ev mono" id="events"></div></div>
  </div>
</main>
<script>
let selected=null, follow=true, lastOverview=null;
const $=id=>document.getElementById(id);
const css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const fmtS=s=>s==null?'—':(s<10?s.toFixed(2):s.toFixed(1))+' s';
const pct=x=>x==null?'—':(100*x).toFixed(0)+'%';
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function pillFor(st){ if(!st) return ['—','p-mute'];
  if(st.startsWith('COMPLETED')&&!st.includes('UNVERIFIED')) return [st,'p-ok'];
  if(st.startsWith('ABORTED')&&!st.includes('UNVERIFIED')) return [st,'p-warn'];
  if(st.includes('FAILED')||st.includes('UNVERIFIED')) return [st,'p-bad'];
  if(st==='PRECHECK_FAILED'||st==='CANCELLED') return [st,'p-mute'];
  return ['RUNNING · '+st,'p-info']; }

async function overview(){
  const o=await (await fetch('/api/overview')).json(); lastOverview=o;
  if(follow && o.runs.length) selected=o.runs[0].id;
  const c=o.cluster||{};
  if(c.error){ $('clusterPill').className='pill p-bad'; $('clusterPill').textContent='cluster unreachable'; $('deps').innerHTML='<tr><td class="mono">'+esc(c.error)+'</td></tr>'; }
  else { const app=c.deployments.filter(d=>d.want>0||d.name!=='loadgenerator');
    const bad=app.filter(d=>d.ready<d.want||d.want===0&&d.name!=='loadgenerator');
    $('clusterPill').className='pill '+(bad.length?'p-bad':'p-ok');
    $('clusterPill').textContent=bad.length?bad.length+' deployment(s) degraded':'all deployments ready';
    $('deps').innerHTML='<tr><th>Deployment</th><th>Ready</th></tr>'+c.deployments.map(d=>{
      const col=d.name==='loadgenerator'?'var(--mute)':(d.ready>=d.want&&d.want>0?'var(--ok)':'var(--bad)');
      return `<tr><td><span class="dot" style="background:${col}"></span>${esc(d.name)}</td><td class="mono">${d.ready}/${d.want}</td></tr>`}).join(''); }
  let b='';
  if(o.active_fault) b+=`<div class="banner b-bad">Active fault: ${esc(o.active_fault.target)} (run ${esc(o.active_fault.run_id)}). If no controller is running, use python chaos\\restore.py</div>`;
  if(o.kill_armed) b+='<div class="banner b-warn">Kill switch file chaos/KILL is present — new runs will refuse to start until it is removed.</div>';
  $('banners').innerHTML=b;
  $('runs').innerHTML='<tr><th>Run</th><th>Target</th><th>Status</th><th>Fault</th><th>Errors in fault</th><th>User recovery</th></tr>'+
    o.runs.map(r=>{const [t,cl]=pillFor(r.status); const fw=r.fault_window||{}; const rec=r.recovery||{};
      const urec=fw.errors===0?'no user impact':(rec.verified?fmtS(rec.user_recovery_s):'—');
      return `<tr class="click ${r.id===selected?'sel':''}" data-id="${esc(r.id)}"><td class="mono">${esc(r.id.slice(0,16))}</td><td>${esc(r.target||'')}</td>
      <td><span class="pill ${cl}">${esc(t)}</span></td><td>${fmtS(r.fault_duration_s)}</td>
      <td>${fw.n!=null?fw.errors+' / '+fw.n:'—'}</td><td>${urec}</td></tr>`}).join('');
  document.querySelectorAll('#runs tr.click').forEach(tr=>tr.onclick=()=>{selected=tr.dataset.id; follow=(selected===o.runs[0].id); detail();});
}

function setupCanvas(cv){const r=window.devicePixelRatio||1, w=cv.clientWidth, h=+cv.getAttribute('height');
  cv.width=w*r; cv.height=h*r; cv.style.height=h+'px'; const g=cv.getContext('2d'); g.scale(r,r); return [g,w,h];}

function draw(d){
  const P=d.probes, s=d.summary, ev=d.events;
  const find=n=>{const e=ev.find(x=>x.event===n); return e?Date.parse(e.ts)/1000:null};
  const tInj=s&&s.fault_injected_at?Date.parse(s.fault_injected_at)/1000:find('fault_injected');
  const tRes=s&&s.restore_issued_at?Date.parse(s.restore_issued_at)/1000:(find('restore_ok'));
  const tRec=s&&s.recovery&&s.recovery.recovered_at&&s.fault_window&&s.fault_window.errors?Date.parse(s.recovery.recovered_at)/1000:null;
  const thr=s?s.request.max_error_rate:(ev[0]&&ev[0].max_error_rate)||0.2;
  const t0=tInj||(P.length?P[0][0]:0);
  const xs=P.map(p=>p[0]-t0); const xmin=xs.length?Math.min(...xs):-1, xmax=xs.length?Math.max(...xs,xmin+5):5;
  // rolling 5 s error rate
  const er=[]; let j=0; for(let i=0;i<P.length;i++){ while(P[i][0]-P[j][0]>5) j++; let bad=0; for(let k=j;k<=i;k++) if(!P[k][1]) bad++; er.push(bad/(i-j+1)); }
  const marks=[]; if(tInj) marks.push([tInj-t0,'fault',css('--warn')]); if(tRes) marks.push([tRes-t0,'restore',css('--ok')]);
  const k8s=s&&s.recovery&&s.recovery.k8s_ready_s!=null&&tRes?tRes+s.recovery.k8s_ready_s:null; if(k8s) marks.push([k8s-t0,'k8s ready',css('--mute')]); if(tRec) marks.push([tRec-t0,'users recovered',css('--info')]);
  const pad={l:52,r:12,t:10,b:22};
  function frame(cv,ymax,ylabel,fmt){ const [g,w,h]=setupCanvas(cv); const X=x=>pad.l+(x-xmin)/(xmax-xmin)*(w-pad.l-pad.r), Y=y=>h-pad.b-(y/ymax)*(h-pad.t-pad.b);
    g.strokeStyle=css('--grid'); g.fillStyle=css('--mute'); g.font='11px system-ui'; g.lineWidth=1;
    for(let i=0;i<=4;i++){const y=ymax*i/4; g.beginPath(); g.moveTo(pad.l,Y(y)); g.lineTo(w-pad.r,Y(y)); g.stroke(); g.fillText(fmt(y),4,Y(y)+4);}
    const step=Math.max(5,Math.ceil((xmax-xmin)/10/5)*5); for(let x=Math.ceil(xmin/step)*step;x<=xmax;x+=step){g.fillText((x>0?'+':'')+x+'s',X(x)-10,h-6);}
    marks.forEach(([x,l,c])=>{g.strokeStyle=c; g.setLineDash([4,3]); g.beginPath(); g.moveTo(X(x),pad.t); g.lineTo(X(x),h-pad.b); g.stroke(); g.setLineDash([]); g.fillStyle=c; g.fillText(l,X(x)+4,pad.t+10);});
    g.fillStyle=css('--mute'); return [g,X,Y,w,h]; }
  const lmax=Math.max(500,...P.map(p=>p[2]))*1.1;
  let [g,X,Y]=frame($('lat'),lmax,'ms',v=>Math.round(v)+'ms');
  P.forEach((p,i)=>{g.fillStyle=p[1]?css('--info'):css('--bad'); const x=X(xs[i]),y=Y(p[2]);
    if(p[1]){g.beginPath(); g.arc(x,y,2.2,0,7); g.fill();} else {g.strokeStyle=css('--bad'); g.lineWidth=1.6; g.beginPath(); g.moveTo(x-3,y-3); g.lineTo(x+3,y+3); g.moveTo(x+3,y-3); g.lineTo(x-3,y+3); g.stroke();}});
  [g,X,Y]=frame($('err'),1,'%',v=>Math.round(v*100)+'%');
  g.strokeStyle=css('--mute'); g.setLineDash([2,3]); g.beginPath(); g.moveTo(X(xmin),Y(thr)); g.lineTo(X(xmax),Y(thr)); g.stroke(); g.setLineDash([]);
  g.strokeStyle=css('--bad'); g.lineWidth=2; g.beginPath(); er.forEach((e,i)=>{const x=X(xs[i]),y=Y(e); i?g.lineTo(x,y):g.moveTo(x,y)}); g.stroke();
  // KPIs: live = last 5 s of probes
  const tl=P.length?P[P.length-1][0]:0, w5=P.filter(p=>tl-p[0]<=5);
  const lat=w5.map(p=>p[2]).sort((a,b)=>a-b); const p95=lat.length?lat[Math.max(0,Math.ceil(.95*lat.length)-1)]:null;
  const e5=w5.length?w5.filter(p=>!p[1]).length/w5.length:null;
  $('kErr').textContent=pct(e5); $('kErr').style.color=e5>thr?css('--bad'):(e5>0?css('--warn'):css('--ok'));
  $('kP95').textContent=p95==null?'—':Math.round(p95)+' ms';
  $('kFault').textContent=s?fmtS(s.fault_duration_s):(tInj?fmtS((tRes||Date.now()/1000)-tInj)+(tRes?'':' …'):'—');
  const rec=s&&s.recovery; $('kRec').textContent=s&&s.fault_window&&s.fault_window.errors===0?'no impact':(rec&&rec.verified?fmtS(rec.user_recovery_s):(tRes?'waiting …':'—'));
  $('kRecSub').textContent=rec&&rec.k8s_ready_s!=null?'(Kubernetes ready after '+fmtS(rec.k8s_ready_s)+')':'';
  $('kErrSub').textContent=s?'Error rate (last 5 s of run)':'Error rate (last 5 s, live)';
  $('kP95Sub').textContent=s?'P95 latency (last 5 s of run)':'P95 latency (last 5 s, live)';
  const [txt,cl]=pillFor(s?s.status:(ev.length?ev[ev.length-1].event:null)); $('runPill').className='pill '+cl; $('runPill').textContent=txt;
  $('chartTitle').textContent='Probe timeline · '+d.id;
  $('events').innerHTML=ev.slice().reverse().map(e=>{const k=Object.entries(e).filter(([a])=>a!=='ts'&&a!=='event').map(([a,v])=>a+'='+(typeof v==='number'?+v.toFixed(3):v)).join(' ');
    const col={fault_injected:'--warn',threshold_breach:'--bad',kill_switch:'--bad',restore_ok:'--ok',recovered:'--info',cleanup_failed:'--bad',telemetry_lost:'--bad'}[e.event];
    return `<div><span style="color:var(--mute)">${esc(e.ts.slice(11,23))}</span> <b style="color:${col?'var('+col+')':'inherit'}">${esc(e.event)}</b> ${esc(k)}</div>`}).join('');
}

async function detail(){ if(!selected){ $('chartTitle').textContent='No runs yet — start chaos/controller.py'; return;}
  const r=await fetch('/api/run/'+encodeURIComponent(selected)); if(r.ok) draw(await r.json()); }
async function tick(){ try{ await overview(); await detail(); }catch(e){ $('runPill').className='pill p-bad'; $('runPill').textContent='dashboard server offline'; } }
tick(); setInterval(tick,1000); window.addEventListener('resize',detail);
</script></body></html>"""


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Chaos dashboard: http://{HOST}:{PORT}   (read-only; Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
