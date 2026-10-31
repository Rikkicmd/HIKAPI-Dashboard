#!/usr/bin/env python3
"""
Hikvision ISAPI Control Panel - local helper
© 2026 Rikki Keene

Serves hik_isapi_tool.html on http://127.0.0.1:8088 and relays the panel's
ISAPI requests to the DVR/NVR, handling Digest/Basic authentication.

Why a helper? Browsers block a web page from calling a recorder directly
(no CORS headers on the device, Digest auth, mixed http/https), so the page
talks to this script and this script talks to the recorder.

Usage:  python hik_isapi_tool.py            (opens your browser)
        python hik_isapi_tool.py --port 9000 --no-browser
Requires only the Python 3.8+ standard library.
"""
import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(HERE, "hik_isapi_tool.html")
TOKEN = secrets.token_urlsafe(24)
ALLOWED_HOSTS = set()
CHECK_HOST = True


# ---------------------------------------------------------------- auth ----
def _parse_challenge(text):
    return {k.lower(): (a if a else b)
            for k, a, b in re.findall(r'(\w+)\s*=\s*(?:"([^"]*)"|([^\s,]+))', text)}


def build_auth(challenges, user, pw, method, uri):
    """Build an Authorization header from the device's WWW-Authenticate challenge(s)."""
    digest = [c for c in challenges if c.strip().lower().startswith("digest")]
    # Prefer the strongest algorithm the device offers (some newer NVRs accept SHA-256 only)
    def _rank(c):
        alg = (_parse_challenge(c.strip()[6:]).get("algorithm") or "MD5").upper()
        return 2 if alg.startswith("SHA-256") else 1 if alg.startswith("SHA-512-256") else 0
    digest.sort(key=_rank, reverse=True)
    if digest:
        p = _parse_challenge(digest[0].strip()[6:])
        realm, nonce = p.get("realm", ""), p.get("nonce", "")
        alg = (p.get("algorithm") or "MD5").upper()
        hname = ("sha512_256" if alg.startswith("SHA-512-256") else
                 "sha256" if alg.startswith("SHA-256") else "md5")
        H = lambda s: hashlib.new(hname, s.encode("utf-8")).hexdigest()
        cnonce = secrets.token_hex(8)
        nc = "00000001"
        qop_opts = [q.strip() for q in (p.get("qop") or "").split(",") if q.strip()]
        qop = "auth" if "auth" in qop_opts else (qop_opts[0] if qop_opts else "")
        ha1 = H(f"{user}:{realm}:{pw}")
        if alg.endswith("-SESS"):
            ha1 = H(f"{ha1}:{nonce}:{cnonce}")
        ha2 = H(f"{method}:{uri}")
        if qop:
            resp = H(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        else:
            resp = H(f"{ha1}:{nonce}:{ha2}")
        parts = [f'username="{user}"', f'realm="{realm}"', f'nonce="{nonce}"',
                 f'uri="{uri}"', f'algorithm={p.get("algorithm") or "MD5"}',
                 f'response="{resp}"']
        if qop:
            parts += [f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"']
        if "opaque" in p:
            parts.append(f'opaque="{p["opaque"]}"')
        return "Digest " + ", ".join(parts)
    if any(c.strip().lower().startswith("basic") for c in challenges):
        return "Basic " + base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode()
    return None


def device_request(spec):
    """Send one request to the device. Retries exactly once after a 401 challenge
    (so a wrong password counts as a single failed login, not several)."""
    scheme = (spec.get("scheme") or "http").lower()
    host = (spec.get("host") or "").strip()
    if not host:
        raise ValueError("No device IP / host entered")
    port = int(spec.get("port") or (443 if scheme == "https" else 80))
    method = (spec.get("method") or "GET").upper()
    path = spec.get("path") or "/"
    if not path.startswith("/"):
        path = "/" + path
    body = spec.get("body") or ""
    data = body.encode("utf-8") if body and method != "GET" else None
    timeout = float(spec.get("timeout") or 20)
    user, pw = spec.get("username") or "", spec.get("password") or ""

    headers = {"Accept": "*/*", "User-Agent": "HikISAPI-ControlPanel/1.0"}
    if data is not None:
        headers["Content-Type"] = spec.get("contentType") or 'application/xml; charset="UTF-8"'
    elif method in ("PUT", "POST"):
        headers["Content-Length"] = "0"

    def connect():
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout,
                                               context=ssl._create_unverified_context())
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def send(auth=None):
        conn = connect()
        h = dict(headers)
        if auth:
            h["Authorization"] = auth
        conn.request(method, path, body=data, headers=h)
        return conn, conn.getresponse()

    conn, resp = send()
    if resp.status == 401 and user:
        challenges = resp.headers.get_all("WWW-Authenticate") or []
        resp.read()
        conn.close()
        auth = build_auth(challenges, user, pw, method, path)
        if auth:
            conn, resp = send(auth)
    return conn, resp


# -------------------------------------------------------------- server ----
class Handler(BaseHTTPRequestHandler):
    server_version = "HikISAPITool/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("  " + (fmt % args) + "\n")

    def _host_ok(self):
        return (not CHECK_HOST) or (self.headers.get("Host", "") in ALLOWED_HOSTS)

    def _json(self, code, obj, proxy_error=False):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if proxy_error:
            self.send_header("X-Proxy-Error", "1")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "Bad Host header"}, True)
        if self.path in ("/", "/index.html"):
            try:
                with open(HTML_FILE, "r", encoding="utf-8") as f:
                    html = f.read().replace("__HIK_TOOL_TOKEN__", TOKEN)
            except FileNotFoundError:
                return self._json(500, {"error": "hik_isapi_tool.html not found next to the script"}, True)
            raw = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
        elif self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self._json(404, {"error": "Not found"}, True)

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "Bad Host header"}, True)
        if self.headers.get("X-Tool-Token") != TOKEN:
            return self._json(403, {"error": "Missing or invalid tool token - reload the page"}, True)
        if self.path == "/api/ping":
            return self._json(200, {"ok": True})
        if self.path != "/api/request":
            return self._json(404, {"error": "Not found"}, True)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            spec = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._json(400, {"error": f"Bad request: {e}"}, True)

        try:
            conn, resp = device_request(spec)
        except socket.timeout:
            return self._json(504, {"error": "Timed out waiting for the device"}, True)
        except ConnectionRefusedError:
            return self._json(502, {"error": "Connection refused - check IP, port and http/https"}, True)
        except ssl.SSLError as e:
            return self._json(502, {"error": f"TLS error: {e}. Try http, or the device's HTTPS port"}, True)
        except Exception as e:
            return self._json(502, {"error": f"{type(e).__name__}: {e}"}, True)

        try:
            self.send_response(200)
            self.send_header("Content-Type", resp.getheader("Content-Type") or "application/octet-stream")
            self.send_header("X-Device-Status", str(resp.status))
            self.send_header("X-Device-Reason", (resp.reason or "").encode("latin-1", "replace").decode("latin-1"))
            self.send_header("X-Device-Headers", json.dumps(dict(resp.getheaders())))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            reader = resp.read1 if hasattr(resp, "read1") else (lambda n: resp.read(n))
            while True:
                chunk = reader(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser stopped the request (e.g. "Stop" on the alert stream)
        except socket.timeout:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


def main():
    global CHECK_HOST
    ap = argparse.ArgumentParser(description="Hikvision ISAPI Control Panel helper")
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="Interface to listen on (default 127.0.0.1 = this PC only)")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    for h in ("127.0.0.1", "localhost", "[::1]"):
        ALLOWED_HOSTS.add(f"{h}:{a.port}")
    if a.bind not in ("127.0.0.1", "localhost", "::1"):
        CHECK_HOST = False
        print("WARNING: listening beyond this PC. Anyone who can open the page can relay to your devices.")

    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{a.port}/"
    print(f"\n  Hikvision ISAPI Control Panel running at {url}\n  Press Ctrl+C to stop.\n")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
