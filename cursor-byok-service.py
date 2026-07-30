#!/usr/bin/env python3
"""Cursor BYOK Service — unified proxy + cloudflared tunnel (pure stdlib).

Single source of truth: config.json in the script directory.
At startup, the script derives cloudflared's config (cf-config.yml) from
config.json, so you only ever edit one file. No separate ~/.cloudflared/config.yml.

The service runs two things in one process:
  1. HTTP proxy that rewrites model aliases to real Bailian model IDs
  2. cloudflared tunnel subprocess (auto-restart on crash)
No pip install, no venv — uses only Python standard library.
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


# ---------------------------------------------------------------------------
# Cloudflared config derivation — generated from config.json at startup
# ---------------------------------------------------------------------------

def _lookup_tunnel_uuid(tunnel_name, cf_bin, credentials_dir):
    """Run cloudflared tunnel list and parse the UUID for tunnel_name."""
    try:
        result = subprocess.run(
            [cf_bin, "tunnel", "list", "--output", "json"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            tunnels = json.loads(result.stdout)
            for t in tunnels:
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
    """Find the tunnel credentials JSON file."""
    candidate = os.path.join(credentials_dir, f"{uuid}.json")
    if os.path.exists(candidate):
        return candidate
    if os.path.isdir(credentials_dir):
        for f in os.listdir(credentials_dir):
            if f.startswith(uuid) and f.endswith(".json"):
                return os.path.join(credentials_dir, f)
    return candidate


def _generate_cf_config():
    """Derive cf-config.yml from config.json values. Called at startup."""
    tunnel_name = CFG["tunnel_name"]
    cf_bin = CFG["cloudflared_bin"]
    credentials_dir = CFG["cf_credentials_dir"]
    hostname = CFG["hostname"]
    listen_port = CFG["listen_port"]

    uuid = _lookup_tunnel_uuid(tunnel_name, cf_bin, credentials_dir)
    if not uuid:
        print(f"[ERROR] tunnel '{tunnel_name}' not found via cloudflared list", flush=True)
        print(f"[INFO] Run: {cf_bin} tunnel create {tunnel_name}", flush=True)
        return False

    creds_file = _find_credentials_file(uuid, credentials_dir)

    yml = (
        f"tunnel: {uuid}\n"
        f"credentials-file: {creds_file}\n"
        f"\n"
        f"ingress:\n"
        f"  - hostname: {hostname}\n"
        f"    service: http://localhost:{listen_port}\n"
        f"  - service: http_status:404\n"
    )
    with open(_CF_CFG_PATH, "w") as f:
        f.write(yml)
    print(f"[INFO] generated cf-config.yml (tunnel={uuid}, hostname={hostname})", flush=True)
    return True


# ---------------------------------------------------------------------------
# HTTP proxy handler
# ---------------------------------------------------------------------------

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            models = [{"id": a, "object": "model"} for a in CFG["model_map"]]
            self._send_json(200, {"object": "list", "data": models})
        elif self.path in ("/", "/health"):
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length > 0 else b""

        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        original = body.get("model", "")
        resolved = CFG["model_map"].get(original, original)
        if resolved != original:
            print(f"[INFO] model rewrite: {original} -> {resolved}", flush=True)
        body["model"] = resolved
        is_stream = body.get("stream", False)

        parsed = urlparse(CFG["bailian_base_url"])
        upstream_path = parsed.path.rstrip("/") + "/chat/completions"
        auth = (f"Bearer {CFG['bailian_api_key']}"
                if CFG["bailian_api_key"]
                else self.headers.get("Authorization", ""))

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
            self._send_json(502, {"error": f"upstream error: {exc}"})
            return

        try:
            self.send_response(resp.status)
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

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}", flush=True)


class _ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# Cloudflared tunnel subprocess management
# ---------------------------------------------------------------------------

def _run_tunnel():
    if not CFG.get("run_tunnel", True):
        return

    if not _generate_cf_config():
        print("[ERROR] cannot start tunnel without valid config", flush=True)
        return

    bin_path = CFG["cloudflared_bin"]
    log_path = os.path.join(_DIR, "tunnel.log")

    while not _shutdown.is_set():
        print(f"[INFO] starting cloudflared with config {_CF_CFG_PATH}", flush=True)
        try:
            log_file = open(log_path, "a")
            proc = subprocess.Popen(
                [bin_path, "tunnel", "--config", _CF_CFG_PATH, "run"],
                stdout=log_file, stderr=subprocess.STDOUT,
            )
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
                log_file.close()
                return

        log_file.close()
        print(f"[WARN] cloudflared exited (code {proc.returncode}), "
              f"restarting in 10s", flush=True)
        _shutdown.wait(timeout=10)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    port = int(CFG["listen_port"])

    tunnel_thread = threading.Thread(target=_run_tunnel, daemon=True)
    tunnel_thread.start()

    server = _ThreadingServer(("127.0.0.1", port), ProxyHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print(f"[INFO] Cursor BYOK proxy on http://127.0.0.1:{port}/v1", flush=True)
    print(f"[INFO] models: {list(CFG['model_map'])}", flush=True)
    print(f"[INFO] public URL: https://{CFG['hostname']}/v1", flush=True)

    def _handle_signal(signum, _frame):
        print(f"[INFO] signal {signum}, shutting down", flush=True)
        _shutdown.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _shutdown.wait()
    server.shutdown()
    server.server_close()
    print("[INFO] service stopped", flush=True)


if __name__ == "__main__":
    main()
