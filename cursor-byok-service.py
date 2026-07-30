#!/usr/bin/env python3
"""Cursor BYOK Service — unified proxy + cloudflared tunnel + web console.

Single source of truth: config.json in the script directory.
At startup, derives cloudflared config (cf-config.yml) from config.json.
Web console at /admin for status, config editing, and restart.
Pure stdlib — no pip, no venv, no npm.
"""

import http.client
import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Config — config.json is the SINGLE source of truth
# ---------------------------------------------------------------------------

_DIR = os.path.dirname(os.path.abspath(__file__))
_CFG_PATH = os.path.join(_DIR, "config.json")
_CF_CFG_PATH = os.path.join(_DIR, "cf-config.yml")

_DEFAULTS = {
    "model_map": {"bl-llm-1": "glm-5.2", "bl-llm-2": "kimi-k2.7-code"},
    "bailian_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "bailian_api_key": "",
    "listen_port": 8787,
    "tunnel_name": "bailian-proxy",
    "hostname": "cursor.alexwuyh.dpdns.org",
    "cloudflared_bin": "/opt/homebrew/bin/cloudflared",
    "cf_credentials_dir": "",
    "run_tunnel": True,
}


def _load_config():
    cfg = dict(_DEFAULTS)
    if os.path.exists(_CFG_PATH):
        try:
            with open(_CFG_PATH) as f:
                user_cfg = json.load(f)
                cfg.update(user_cfg)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[WARN] config.json unreadable: {exc}", file=sys.stderr)
    if os.environ.get("BAILIAN_API_KEY"):
        cfg["bailian_api_key"] = os.environ["BAILIAN_API_KEY"]
    if not cfg.get("cf_credentials_dir"):
        cfg["cf_credentials_dir"] = os.path.expanduser("~/.cloudflared")
    return cfg


CFG = _load_config()
_shutdown = threading.Event()

# Stats tracking
_stats_lock = threading.Lock()
_stats = {
    "start_time": time.time(),
    "tunnel_pid": None,
    "requests": [],
}


def _log_request(model, status, latency_ms):
    with _stats_lock:
        _stats["requests"].append({
            "time": time.strftime("%H:%M:%S"),
            "model": model,
            "status": status,
            "latency_ms": round(latency_ms),
        })
        if len(_stats["requests"]) > 50:
            _stats["requests"] = _stats["requests"][-50:]


# ---------------------------------------------------------------------------
# Cloudflared config derivation
# ---------------------------------------------------------------------------

def _lookup_tunnel_uuid(tunnel_name, cf_bin, credentials_dir):
    try:
        result = subprocess.run(
            [cf_bin, "tunnel", "list", "--output", "json"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            for t in json.loads(result.stdout):
                if t.get("name") == tunnel_name:
                    return t.get("id")
    except (json.JSONDecodeError, subprocess.TimeoutExpired, OSError):
        pass
    try:
        result = subprocess.run(
            [cf_bin, "tunnel", "list"],
            capture_output=True, text=True, timeout=15,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == tunnel_name:
                return parts[0]
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _find_credentials_file(uuid, credentials_dir):
    candidate = os.path.join(credentials_dir, f"{uuid}.json")
    if os.path.exists(candidate):
        return candidate
    if os.path.isdir(credentials_dir):
        for f in os.listdir(credentials_dir):
            if f.startswith(uuid) and f.endswith(".json"):
                return os.path.join(credentials_dir, f)
    return candidate


def _generate_cf_config():
    uuid = _lookup_tunnel_uuid(
        CFG["tunnel_name"], CFG["cloudflared_bin"], CFG["cf_credentials_dir"])
    if not uuid:
        print(f"[ERROR] tunnel '{CFG['tunnel_name']}' not found", flush=True)
        return False
    creds = _find_credentials_file(uuid, CFG["cf_credentials_dir"])
    yml = (
        f"tunnel: {uuid}\n"
        f"credentials-file: {creds}\n\n"
        f"ingress:\n"
        f"  - hostname: {CFG['hostname']}\n"
        f"    service: http://localhost:{CFG['listen_port']}\n"
        f"  - service: http_status:404\n"
    )
    with open(_CF_CFG_PATH, "w") as f:
        f.write(yml)
    print(f"[INFO] cf-config.yml (tunnel={uuid}, host={CFG['hostname']})", flush=True)
    return True


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

def _resolve_model(alias):
    entry = CFG["model_map"].get(alias)
    if entry is None:
        return alias, CFG["bailian_base_url"], CFG["bailian_api_key"]
    if isinstance(entry, str):
        return entry, CFG["bailian_base_url"], CFG["bailian_api_key"]
    if isinstance(entry, dict):
        return (entry.get("model_id", alias),
                entry.get("base_url", CFG["bailian_base_url"]),
                entry.get("api_key", CFG["bailian_api_key"]))
    return alias, CFG["bailian_base_url"], CFG["bailian_api_key"]


# ---------------------------------------------------------------------------
# Admin console HTML
# ---------------------------------------------------------------------------

_ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BYOK Console</title>
<style>
:root{--bg:#1a1a1a;--surface:#242424;--border:#333;--text:#e0e0e0;--muted:#888;--accent:#4a9eff;--success:#4caf50;--danger:#f44336;--warn:#f39c12;--radius:6px;--mono:'SF Mono',Menlo,Consolas,monospace}
*{margin:0;padding:0;box-sizing:border-box}
body{font:14px/1.5 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);padding:20px;max-width:920px;margin:0 auto}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid var(--border)}
header h1{font-size:18px;font-weight:600}
.badges{display:flex;gap:14px;flex-wrap:wrap}
.badge{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--muted)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex-shrink:0}
.dot-ok{background:var(--success)}.dot-err{background:var(--danger)}.dot-warn{background:var(--warn)}
section{margin-bottom:28px}
h2{font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px}
label{display:block;font-size:12px;color:var(--muted);margin:8px 0 4px}
input,select{width:100%;padding:7px 10px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);font:13px var(--mono);outline:none}
input:focus{border-color:var(--accent)}
.btn{display:inline-block;padding:8px 16px;background:var(--accent);color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:13px;font-weight:500}
.btn:hover{opacity:.85}.btn-danger{background:var(--danger)}.btn-sm{padding:4px 10px;font-size:11px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.actions{display:flex;gap:8px;margin-top:14px}
.muted{color:var(--muted);font-size:12px}
.model-row{display:grid;grid-template-columns:1fr 1fr 1.4fr 1.4fr auto;gap:6px;align-items:end;margin-bottom:8px}
.model-row input{font-size:12px}
.logs{font:12px var(--mono);color:var(--muted);max-height:220px;overflow-y:auto}
.le{padding:2px 0}.le .t{color:var(--muted)}.le .ok{color:var(--success)}.le .er{color:var(--danger)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid var(--border)}
th{color:var(--muted);font-weight:500;font-size:12px}
td code{font-family:var(--mono);font-size:12px}
#overlay{text-align:center;padding:60px;color:var(--muted);font-size:16px}
</style>
</head>
<body>
<header>
  <h1>BYOK Console</h1>
  <div class="badges" id="badges"></div>
</header>
<section>
  <h2>Models</h2>
  <div class="card">
    <div id="model-rows"></div>
    <div class="actions">
      <button class="btn btn-sm" onclick="addModelRow()">+ Add Model</button>
    </div>
  </div>
</section>
<section>
  <h2>Global Config</h2>
  <div class="card">
    <div class="grid2">
      <div><label>Default Base URL</label><input id="cfg-base-url" placeholder="https://..."></div>
      <div><label>Public Hostname</label><input id="cfg-hostname" placeholder="cursor.domain.com"></div>
      <div><label>Default API Key</label><input id="cfg-api-key" type="password" placeholder="sk-..."></div>
      <div><label>Tunnel Name</label><input id="cfg-tunnel-name" placeholder="bailian-proxy"></div>
      <div><label>Listen Port</label><input id="cfg-port" type="number" value="8787"></div>
      <div><label>Run Tunnel</label><select id="cfg-run-tunnel"><option value="true">Enabled</option><option value="false">Disabled</option></select></div>
    </div>
    <div class="actions">
      <button class="btn" onclick="saveRestart()">Save &amp; Restart</button>
      <button class="btn btn-danger" onclick="restartOnly()">Restart Only</button>
    </div>
  </div>
</section>
<section>
  <h2>Recent Requests</h2>
  <div class="card"><div class="logs" id="logs">No requests yet.</div></div>
</section>
<script>
let origCfg={};
async function api(p,o){const r=await fetch(p,o);return r.json();}
async function load(){
  const[s,c]=await Promise.all([api('/admin/api/status'),api('/admin/api/config')]);
  renderBadges(s);renderConfig(c);renderModels(c.model_map||{});renderLogs(s.requests||[]);
}
function renderBadges(s){
  const tp=s.tunnel_pid&&s.tunnel_pid>0;
  const up=s.uptime?fmtUptime(s.uptime):'--';
  document.getElementById('badges').innerHTML=
    `<span class="badge"><span class="dot dot-ok"></span>Proxy</span>`+
    `<span class="badge"><span class="dot ${tp?'dot-ok':'dot-err'}"></span>Tunnel ${tp?'OK':'Down'}</span>`+
    `<span class="badge"><span class="dot dot-warn"></span>${up}</span>`+
    `<span class="badge"><span class="dot dot-ok"></span>${s.model_count||0} Models</span>`;
}
function fmtUptime(s){
  if(s<60)return Math.round(s)+'s';
  if(s<3600)return Math.round(s/60)+'m';
  return (s/3600).toFixed(1)+'h';
}
function renderConfig(c){
  origCfg=c;
  val('cfg-base-url',c.bailian_base_url);val('cfg-hostname',c.hostname);
  val('cfg-api-key',c.bailian_api_key);val('cfg-tunnel-name',c.tunnel_name);
  val('cfg-port',c.listen_port||8787);
  document.getElementById('cfg-run-tunnel').value=String(c.run_tunnel);
}
function val(id,v){document.getElementById(id).value=v||'';}
function renderModels(mm){
  const c=document.getElementById('model-rows');c.innerHTML='';
  for(const[a,e]of Object.entries(mm)){
    if(typeof e==='string')addModelRow(a,e,'','');
    else addModelRow(a,e.model_id||'',e.base_url||'',e.api_key||'');
  }
}
function addModelRow(alias='',mid='',bu='',ak=''){
  const c=document.getElementById('model-rows');
  const r=document.createElement('div');r.className='model-row';
  r.innerHTML=`<input placeholder="Alias" value="${esc(alias)}">`+
    `<input placeholder="Model ID" value="${esc(mid)}">`+
    `<input placeholder="Base URL (blank=default)" value="${esc(bu)}">`+
    `<input placeholder="API Key (blank=default)" type="password" value="${esc(ak)}">`+
    `<button class="btn btn-danger btn-sm" onclick="this.parentElement.remove()">Delete</button>`;
  c.appendChild(r);
}
function esc(s){return String(s).replace(/"/g,'&quot;').replace(/</g,'&lt;');}
function collectConfig(){
  const mm={};
  document.querySelectorAll('.model-row').forEach(r=>{
    const i=r.querySelectorAll('input');
    const a=i[0].value.trim();if(!a)return;
    const m=i[1].value.trim(),bu=i[2].value.trim(),ak=i[3].value.trim();
    if(bu||ak){const e={model_id:m};if(bu)e.base_url=bu;if(ak)e.api_key=ak;mm[a]=e;}
    else mm[a]=m;
  });
  return{model_map:mm,
    bailian_base_url:document.getElementById('cfg-base-url').value,
    hostname:document.getElementById('cfg-hostname').value,
    bailian_api_key:document.getElementById('cfg-api-key').value,
    tunnel_name:document.getElementById('cfg-tunnel-name').value,
    listen_port:parseInt(document.getElementById('cfg-port').value)||8787,
    run_tunnel:document.getElementById('cfg-run-tunnel').value==='true',
    cloudflared_bin:origCfg.cloudflared_bin||'',
    cf_credentials_dir:origCfg.cf_credentials_dir||'',
  };
}
async function saveRestart(){
  await api('/admin/api/config',{method:'POST',body:JSON.stringify(collectConfig()),headers:{'Content-Type':'application/json'}});
  await api('/admin/api/restart',{method:'POST'});
  document.body.innerHTML='<div id="overlay">Saving &amp; restarting...<br>launchd will bring the service back in a few seconds.</div>';
  setTimeout(()=>location.reload(),5000);
}
async function restartOnly(){
  await api('/admin/api/restart',{method:'POST'});
  document.body.innerHTML='<div id="overlay">Restarting...</div>';
  setTimeout(()=>location.reload(),5000);
}
function renderLogs(r){
  const e=document.getElementById('logs');
  if(!r.length){e.textContent='No requests yet.';return;}
  e.innerHTML=r.reverse().map(x=>
    `<div class="le"><span class="t">${x.time}</span> `+
    `<span class="${x.status===200?'ok':'er'}">${x.status}</span> `+
    `${esc(x.model)} <span class="muted">${x.latency_ms}ms</span></div>`
  ).join('');
}
load();setInterval(load,5000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP handler — proxy + admin
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, content):
        body = content.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/admin":
            self._html(_ADMIN_HTML)
        elif p == "/admin/api/status":
            with _stats_lock:
                reqs = list(_stats["requests"][-20:])
                pid = _stats["tunnel_pid"]
            self._json(200, {
                "proxy_running": True,
                "tunnel_pid": pid,
                "uptime": time.time() - _stats["start_time"],
                "model_count": len(CFG["model_map"]),
                "requests": reqs,
            })
        elif p == "/admin/api/config":
            safe = dict(CFG)
            self._json(200, safe)
        elif p == "/v1/models":
            models = [{"id": a, "object": "model"} for a in CFG["model_map"]]
            self._json(200, {"object": "list", "data": models})
        elif p in ("/", "/health"):
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]

        # Admin: save config
        if p == "/admin/api/config":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                new_cfg = json.loads(raw)
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid JSON"})
                return
            with open(_CFG_PATH, "w") as f:
                json.dump(new_cfg, f, indent=2, ensure_ascii=False)
                f.write("\n")
            print("[INFO] config.json updated via admin console", flush=True)
            self._json(200, {"status": "saved"})
            return

        # Admin: restart
        if p == "/admin/api/restart":
            self._json(200, {"status": "restarting"})
            threading.Timer(0.5, _shutdown.set).start()
            return

        # Proxy: chat completions
        if p != "/v1/chat/completions":
            self._json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""

        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON body"})
            return

        original = body.get("model", "")
        model_id, base_url, api_key = _resolve_model(original)
        body["model"] = model_id
        is_stream = body.get("stream", False)

        parsed = urlparse(base_url)
        upstream_path = parsed.path.rstrip("/") + "/chat/completions"
        auth = (f"Bearer {api_key}" if api_key
                else self.headers.get("Authorization", ""))

        t0 = time.time()
        try:
            if parsed.scheme == "https":
                conn = http.client.HTTPSConnection(parsed.netloc, timeout=120)
            else:
                conn = http.client.HTTPConnection(parsed.netloc, timeout=120)
            conn.request("POST", upstream_path,
                         body=json.dumps(body, ensure_ascii=False).encode(),
                         headers={"Content-Type": "application/json",
                                 "Authorization": auth})
            resp = conn.getresponse()
        except Exception as exc:
            _log_request(original, 502, (time.time() - t0) * 1000)
            self._json(502, {"error": f"upstream error: {exc}"})
            return

        status = resp.status
        try:
            self.send_response(status)
            for key, val in resp.getheaders():
                if key.lower() in ("transfer-encoding", "connection",
                                    "content-length"):
                    continue
                self.send_header(key, val)

            if is_stream:
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    while True:
                        chunk = resp.read(4096)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                data = resp.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        finally:
            conn.close()
            _log_request(original, status, (time.time() - t0) * 1000)

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}", flush=True)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# Tunnel management
# ---------------------------------------------------------------------------

def _run_tunnel():
    if not CFG.get("run_tunnel", True):
        return
    if not _generate_cf_config():
        print("[ERROR] cannot start tunnel", flush=True)
        return

    bin_path = CFG["cloudflared_bin"]
    log_path = os.path.join(_DIR, "tunnel.log")

    while not _shutdown.is_set():
        print(f"[INFO] starting cloudflared", flush=True)
        try:
            log_file = open(log_path, "a")
            proc = subprocess.Popen(
                [bin_path, "tunnel", "--config", _CF_CFG_PATH, "run"],
                stdout=log_file, stderr=subprocess.STDOUT,
            )
            with _stats_lock:
                _stats["tunnel_pid"] = proc.pid
        except FileNotFoundError:
            print(f"[ERROR] cloudflared not found at {bin_path}", flush=True)
            return

        while proc.poll() is None:
            if _shutdown.wait(timeout=2):
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                with _stats_lock:
                    _stats["tunnel_pid"] = None
                log_file.close()
                return

        with _stats_lock:
            _stats["tunnel_pid"] = None
        log_file.close()
        print(f"[WARN] cloudflared exited ({proc.returncode}), restart in 10s", flush=True)
        _shutdown.wait(timeout=10)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    port = int(CFG["listen_port"])

    tunnel_thread = threading.Thread(target=_run_tunnel, daemon=True)
    tunnel_thread.start()

    server = _Server(("127.0.0.1", port), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print(f"[INFO] proxy on http://127.0.0.1:{port}/v1", flush=True)
    print(f"[INFO] console at http://127.0.0.1:{port}/admin", flush=True)
    print(f"[INFO] models: {list(CFG['model_map'])}", flush=True)
    print(f"[INFO] public URL: https://{CFG['hostname']}/v1", flush=True)

    def _sig(signum, _frame):
        print(f"[INFO] signal {signum}", flush=True)
        _shutdown.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    _shutdown.wait()
    server.shutdown()
    server.server_close()
    print("[INFO] stopped", flush=True)


if __name__ == "__main__":
    main()
