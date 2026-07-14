#!/usr/bin/env python3
"""
Linux fleet workload monitor.

Runs on a Windows (or any) machine, polls a list of Linux hosts over SSH,
and serves a live browser dashboard showing CPU and memory load per host.

Nothing needs to be installed on the Linux machines — only sshd (already running).

Setup:
    pip install paramiko
    edit hosts.json  (see hosts.example.json)
    python monitor.py

Then open http://localhost:8000 in your browser.
"""

import json
import os
import sys
import time
import threading
import http.server
import socketserver
from concurrent.futures import ThreadPoolExecutor

try:
    import paramiko
except ImportError:
    print("paramiko is required.  Install it with:  pip install paramiko")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOSTS_FILE = os.path.join(BASE_DIR, "hosts.json")
WEB_PORT = 8000
POLL_INTERVAL = 5          # seconds between polling rounds
SSH_TIMEOUT = 8            # per-connection timeout
HISTORY_LEN = 60           # samples kept per host for the mini charts

# One remote command gathers everything in a single round trip.
# We read /proc/stat twice with a short gap to compute CPU %, plus meminfo,
# load average, cpu core count, and uptime.
REMOTE_CMD = (
    "cat /proc/stat | grep '^cpu '; "
    "sleep 0.3; "
    "echo ---; "
    "cat /proc/stat | grep '^cpu '; "
    "echo ---; "
    "cat /proc/meminfo | grep -E '^(MemTotal|MemAvailable):'; "
    "echo ---; "
    "cat /proc/loadavg; "
    "echo ---; "
    "nproc; "
    "echo ---; "
    "cat /proc/uptime"
)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_state = {}          # name -> latest metrics dict
_history = {}        # name -> {"cpu": [...], "mem": [...]}


def load_hosts():
    if not os.path.exists(HOSTS_FILE):
        print(f"No {HOSTS_FILE} found.  Copy hosts.example.json to hosts.json and edit it.")
        sys.exit(1)
    with open(HOSTS_FILE, "r", encoding="utf-8") as f:
        hosts = json.load(f)
    if not isinstance(hosts, list) or not hosts:
        print("hosts.json must be a non-empty JSON array of host objects.")
        sys.exit(1)
    return hosts


def _parse_cpu_line(line):
    # "cpu  user nice system idle iowait irq softirq steal ..."
    parts = line.split()[1:]
    vals = [int(p) for p in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
    total = sum(vals)
    return total, idle


def poll_host(host):
    name = host.get("name") or host["hostname"]
    result = {
        "name": name,
        "hostname": host["hostname"],
        "ok": False,
        "error": None,
        "cpu_pct": None,
        "mem_pct": None,
        "mem_total_gb": None,
        "mem_used_gb": None,
        "load1": None,
        "load5": None,
        "load15": None,
        "cores": None,
        "uptime_h": None,
        "ts": time.time(),
    }
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        connect_kwargs = {
            "hostname": host["hostname"],
            "port": host.get("port", 22),
            "username": host["username"],
            "timeout": SSH_TIMEOUT,
            "banner_timeout": SSH_TIMEOUT,
            "auth_timeout": SSH_TIMEOUT,
        }
        if host.get("key_file"):
            connect_kwargs["key_filename"] = os.path.expanduser(host["key_file"])
        if host.get("password"):
            connect_kwargs["password"] = host["password"]
            connect_kwargs["look_for_keys"] = False
        client.connect(**connect_kwargs)

        stdin, stdout, stderr = client.exec_command(REMOTE_CMD, timeout=SSH_TIMEOUT)
        out = stdout.read().decode("utf-8", "replace")
        blocks = [b.strip() for b in out.split("---")]

        cpu1_total, cpu1_idle = _parse_cpu_line(blocks[0])
        cpu2_total, cpu2_idle = _parse_cpu_line(blocks[1])
        dt = cpu2_total - cpu1_total
        di = cpu2_idle - cpu1_idle
        cpu_pct = 0.0 if dt <= 0 else max(0.0, min(100.0, 100.0 * (dt - di) / dt))

        mem_total_kb = mem_avail_kb = 0
        for line in blocks[2].splitlines():
            if line.startswith("MemTotal"):
                mem_total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable"):
                mem_avail_kb = int(line.split()[1])
        mem_used_kb = mem_total_kb - mem_avail_kb
        mem_pct = 0.0 if mem_total_kb == 0 else 100.0 * mem_used_kb / mem_total_kb

        load_parts = blocks[3].split()
        load1, load5, load15 = (float(load_parts[0]), float(load_parts[1]), float(load_parts[2]))

        cores = int(blocks[4].strip().splitlines()[0])
        uptime_s = float(blocks[5].split()[0])

        result.update({
            "ok": True,
            "cpu_pct": round(cpu_pct, 1),
            "mem_pct": round(mem_pct, 1),
            "mem_total_gb": round(mem_total_kb / 1048576, 1),
            "mem_used_gb": round(mem_used_kb / 1048576, 1),
            "load1": round(load1, 2),
            "load5": round(load5, 2),
            "load15": round(load15, 2),
            "cores": cores,
            "uptime_h": round(uptime_s / 3600, 1),
        })
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        client.close()
    return result


def poller_loop(hosts):
    with ThreadPoolExecutor(max_workers=min(16, len(hosts))) as pool:
        while True:
            start = time.time()
            results = list(pool.map(poll_host, hosts))
            with _state_lock:
                for r in results:
                    name = r["name"]
                    _state[name] = r
                    h = _history.setdefault(name, {"cpu": [], "mem": []})
                    if r["ok"]:
                        h["cpu"].append(r["cpu_pct"])
                        h["mem"].append(r["mem_pct"])
                        h["cpu"] = h["cpu"][-HISTORY_LEN:]
                        h["mem"] = h["mem"][-HISTORY_LEN:]
            elapsed = time.time() - start
            time.sleep(max(0, POLL_INTERVAL - elapsed))


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet

    def _send(self, code, content, ctype):
        body = content.encode("utf-8") if isinstance(content, str) else content
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif self.path.startswith("/api/data"):
            with _state_lock:
                payload = {
                    "hosts": sorted(_state.values(), key=lambda r: r["name"].lower()),
                    "history": _history,
                    "interval": POLL_INTERVAL,
                    "server_time": time.time(),
                }
            self._send(200, json.dumps(payload), "application/json")
        else:
            self._send(404, "not found", "text/plain")


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------------------
# Embedded dashboard
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fleet workload</title>
<style>
  :root{
    --bg:#0d1117; --panel:#151b24; --panel-2:#1b232e;
    --line:#232c38; --line-hi:#31404f;
    --ink:#e6edf3; --ink-dim:#8b98a5; --ink-faint:#5b6673;
    --teal:#2dd4a7; --teal-dim:#155e4b;
    --amber:#f0a92a; --amber-dim:#6b4a0f;
    --red:#f2555a; --red-dim:#6b1f22;
    --mono:"SFMono-Regular",ui-monospace,"Cascadia Mono","JetBrains Mono",Menlo,Consolas,monospace;
    --sans:"Inter",system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans)}
  body{padding:28px clamp(16px,4vw,48px) 64px}
  a{color:inherit}

  header{display:flex;align-items:baseline;gap:18px;flex-wrap:wrap;
         border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:24px}
  h1{font-size:15px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;margin:0}
  h1 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--teal);
          margin-right:10px;vertical-align:middle;box-shadow:0 0 0 4px rgba(45,212,167,.14)}
  .meta{font-family:var(--mono);font-size:12px;color:var(--ink-faint);display:flex;gap:20px;flex-wrap:wrap}
  .meta b{color:var(--ink-dim);font-weight:500}
  .spacer{flex:1}
  .toggle{font-family:var(--mono);font-size:12px;color:var(--ink-dim);cursor:pointer;
          border:1px solid var(--line-hi);background:var(--panel);padding:7px 12px;border-radius:7px;
          user-select:none;transition:border-color .15s,color .15s}
  .toggle:hover{border-color:var(--teal-dim);color:var(--ink)}
  .toggle.on{color:var(--teal);border-color:var(--teal-dim)}

  .grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}

  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px 14px;
        position:relative;overflow:hidden;transition:border-color .2s}
  .card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--edge,var(--line-hi))}
  .card.s-idle{--edge:var(--teal)} .card.s-busy{--edge:var(--amber)}
  .card.s-hot{--edge:var(--red)} .card.s-down{--edge:var(--ink-faint)}
  .card.s-hot{animation:pulse 2.4s ease-in-out infinite}
  @keyframes pulse{0%,100%{border-color:var(--line)}50%{border-color:var(--red-dim)}}
  @media (prefers-reduced-motion:reduce){.card.s-hot{animation:none}}

  .card-top{display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:14px}
  .name{font-size:15px;font-weight:600;letter-spacing:.01em}
  .host{font-family:var(--mono);font-size:11px;color:var(--ink-faint);margin-top:2px}
  .badge{font-family:var(--mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;
         padding:3px 8px;border-radius:5px;white-space:nowrap}
  .badge.s-idle{color:var(--teal);background:rgba(45,212,167,.1)}
  .badge.s-busy{color:var(--amber);background:rgba(240,169,42,.1)}
  .badge.s-hot{color:var(--red);background:rgba(242,85,90,.1)}
  .badge.s-down{color:var(--ink-faint);background:rgba(91,102,115,.12)}

  .metric{margin-bottom:13px}
  .metric-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}
  .metric-label{font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-dim)}
  .metric-val{font-family:var(--mono);font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}
  .metric-sub{font-family:var(--mono);font-size:11px;color:var(--ink-faint);margin-left:7px;font-weight:400}
  .bar{height:7px;background:var(--panel-2);border-radius:4px;overflow:hidden}
  .bar > i{display:block;height:100%;border-radius:4px;transition:width .5s ease, background .3s}

  .spark{margin-top:2px;height:34px;width:100%;display:block}

  .foot{display:flex;justify-content:space-between;font-family:var(--mono);font-size:11px;
        color:var(--ink-faint);border-top:1px solid var(--line);margin-top:12px;padding-top:9px}

  .err{font-family:var(--mono);font-size:12px;color:var(--red);background:rgba(242,85,90,.07);
       border:1px solid var(--red-dim);border-radius:7px;padding:10px 12px;margin-top:4px;
       word-break:break-word;line-height:1.5}
  .empty{color:var(--ink-faint);font-family:var(--mono);font-size:13px;padding:40px 0;text-align:center}
</style>
</head>
<body>
  <header>
    <h1><span class="dot" id="livedot"></span>Fleet workload</h1>
    <div class="meta">
      <span><b id="ok-count">0</b> online</span>
      <span><b id="down-count">0</b> down</span>
      <span>refresh <b id="interval">–</b>s</span>
      <span>updated <b id="updated">–</b></span>
    </div>
    <div class="spacer"></div>
    <div class="toggle on" id="sortToggle">sort: most free first</div>
  </header>

  <div class="grid" id="grid"></div>
  <div class="empty" id="empty" style="display:none">Waiting for first poll…</div>

<script>
const $ = s => document.querySelector(s);
let sortByFree = true;
let lastData = null;

$("#sortToggle").addEventListener("click", () => {
  sortByFree = !sortByFree;
  const el = $("#sortToggle");
  el.classList.toggle("on", sortByFree);
  el.textContent = sortByFree ? "sort: most free first" : "sort: by name";
  if (lastData) render(lastData);
});

function statusOf(h){
  if(!h.ok) return "down";
  const m = Math.max(h.cpu_pct ?? 0, h.mem_pct ?? 0);
  if(m >= 85) return "hot";
  if(m >= 50) return "busy";
  return "idle";
}
const STATUS_LABEL = {idle:"available", busy:"busy", hot:"saturated", down:"offline"};
const barColor = pct => pct>=85 ? "var(--red)" : pct>=50 ? "var(--amber)" : "var(--teal)";

function sparkline(vals, color){
  if(!vals || vals.length < 2) return "";
  const w=280, hgt=34, n=vals.length;
  const pts = vals.map((v,i)=>{
    const x = (i/(n-1))*w;
    const y = hgt - (Math.max(0,Math.min(100,v))/100)*(hgt-2) - 1;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const line = pts.join(" ");
  const area = `0,${hgt} ${line} ${w},${hgt}`;
  const id = "g"+Math.random().toString(36).slice(2,8);
  return `<svg class="spark" viewBox="0 0 ${w} ${hgt}" preserveAspectRatio="none">
    <defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${color}" stop-opacity="0.28"/>
      <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
    </linearGradient></defs>
    <polygon points="${area}" fill="url(#${id})"/>
    <polyline points="${line}" fill="none" stroke="${color}" stroke-width="1.5"
      stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

function card(h, hist){
  const st = statusOf(h);
  if(!h.ok){
    return `<div class="card s-down">
      <div class="card-top">
        <div><div class="name">${h.name}</div><div class="host">${h.hostname}</div></div>
        <div class="badge s-down">offline</div>
      </div>
      <div class="err">${h.error || "unreachable"}</div>
    </div>`;
  }
  const cpuHist = hist ? hist.cpu : [];
  const memHist = hist ? hist.mem : [];
  return `<div class="card s-${st}">
    <div class="card-top">
      <div><div class="name">${h.name}</div><div class="host">${h.hostname} · ${h.cores} cores</div></div>
      <div class="badge s-${st}">${STATUS_LABEL[st]}</div>
    </div>

    <div class="metric">
      <div class="metric-head">
        <span class="metric-label">CPU</span>
        <span class="metric-val" style="color:${barColor(h.cpu_pct)}">${h.cpu_pct.toFixed(0)}<span class="metric-sub">% · load ${h.load1}</span></span>
      </div>
      <div class="bar"><i style="width:${h.cpu_pct}%;background:${barColor(h.cpu_pct)}"></i></div>
      ${sparkline(cpuHist, barColor(h.cpu_pct))}
    </div>

    <div class="metric">
      <div class="metric-head">
        <span class="metric-label">Memory</span>
        <span class="metric-val" style="color:${barColor(h.mem_pct)}">${h.mem_pct.toFixed(0)}<span class="metric-sub">% · ${h.mem_used_gb}/${h.mem_total_gb} GB</span></span>
      </div>
      <div class="bar"><i style="width:${h.mem_pct}%;background:${barColor(h.mem_pct)}"></i></div>
      ${sparkline(memHist, barColor(h.mem_pct))}
    </div>

    <div class="foot">
      <span>load ${h.load1} / ${h.load5} / ${h.load15}</span>
      <span>up ${h.uptime_h}h</span>
    </div>
  </div>`;
}

function freeScore(h){
  if(!h.ok) return -1;                       // down sinks to bottom
  return 100 - Math.max(h.cpu_pct, h.mem_pct); // more free = higher
}

function render(data){
  lastData = data;
  const hosts = data.hosts.slice();
  if(sortByFree){
    hosts.sort((a,b)=> freeScore(b) - freeScore(a));
  }else{
    hosts.sort((a,b)=> a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
  }
  const grid = $("#grid");
  grid.innerHTML = hosts.map(h => card(h, data.history[h.name])).join("");
  $("#empty").style.display = hosts.length ? "none" : "block";

  const ok = data.hosts.filter(h=>h.ok).length;
  $("#ok-count").textContent = ok;
  $("#down-count").textContent = data.hosts.length - ok;
  $("#interval").textContent = data.interval;
  $("#updated").textContent = new Date(data.server_time*1000).toLocaleTimeString();
}

async function tick(){
  const dot = $("#livedot");
  try{
    const r = await fetch("/api/data", {cache:"no-store"});
    const data = await r.json();
    render(data);
    dot.style.background = "var(--teal)";
    dot.style.boxShadow = "0 0 0 4px rgba(45,212,167,.14)";
  }catch(e){
    dot.style.background = "var(--red)";
    dot.style.boxShadow = "0 0 0 4px rgba(242,85,90,.14)";
  }
}

tick();
setInterval(tick, 3000);
</script>
</body>
</html>
"""


def main():
    hosts = load_hosts()
    print(f"Loaded {len(hosts)} host(s).  Polling every {POLL_INTERVAL}s.")
    t = threading.Thread(target=poller_loop, args=(hosts,), daemon=True)
    t.start()
    server = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler)
    print(f"Dashboard:  http://localhost:{WEB_PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
