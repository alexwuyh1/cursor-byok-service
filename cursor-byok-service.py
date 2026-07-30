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
# Admin console — loaded from admin.html at startup
# ---------------------------------------------------------------------------

_ADMIN_HTML_PATH = os.path.join(_DIR, "admin.html")
_ADMIN_HTML = ""
if os.path.exists(_ADMIN_HTML_PATH):
    with open(_ADMIN_HTML_PATH, encoding="utf-8") as _f:
        _ADMIN_HTML = _f.read()
else:
    print("[WARN] admin.html not found, console disabled", file=sys.stderr)



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
