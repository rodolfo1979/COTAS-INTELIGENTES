from __future__ import annotations

import hashlib
import hmac
import html
import http.client
import os
import sqlite3
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
AUTH_SECRET = os.getenv("COTAS_PORTAL_SECRET", os.getenv("COTAS_SUPERADMIN_SECRET", ""))
DB_PATH = Path(os.getenv("COTAS_SUPERADMIN_DB", ROOT / "storage" / "super_admin.sqlite")).resolve()
PORTAL_COOKIE = "cotas_tenant"
APP_COOKIE = "cotas_session"
ACTIVE_STATUSES = {"activo", "prueba"}


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def sign_value(value: str) -> str:
    signature = hmac.new(AUTH_SECRET.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{value}:{signature}"


def valid_signed_value(value: str) -> str:
    if not AUTH_SECRET or ":" not in value:
        return ""
    payload, signature = value.rsplit(":", 1)
    expected = sign_value(payload).rsplit(":", 1)[1]
    return payload if hmac.compare_digest(signature, expected) else ""


def app_session(username: str, tenant_secret: str) -> str:
    signature = hmac.new(tenant_secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{username}:{signature}"


def get_cookie(header: str, name: str) -> str:
    for part in header.split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        if key == name:
            return unquote(value)
    return ""


def find_tenant(slug: str) -> dict | None:
    if not DB_PATH.exists():
        return None
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("select * from tenants where slug = ?", (slug,)).fetchone()
    return dict(row) if row else None


def find_login_tenant(slug: str, username: str, password: str) -> dict | None:
    tenant = find_tenant(slug)
    if not tenant or tenant.get("rent_status") not in ACTIVE_STATUSES:
        return None
    if not hmac.compare_digest(str(tenant.get("app_user", "")), username):
        return None
    if not hmac.compare_digest(str(tenant.get("app_password", "")), password):
        return None
    return tenant


def login_page(message: str = "") -> bytes:
    note = f'<p class="error">{esc(message)}</p>' if message else ""
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>COTAS INTELIGENTES</title>
  <style>
    :root {{ --ink:#172033; --muted:#667085; --line:#d8dee9; --panel:#fff; --paper:#f4f6f8; --accent:#b42318; --dark:#1f2937; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Arial, Helvetica, sans-serif; color:var(--ink); background:var(--paper); }}
    header {{ background:var(--dark); color:#fff; padding:18px 24px; }}
    header h1 {{ margin:0; font-size:19px; letter-spacing:0; }}
    main {{ width:min(820px, calc(100vw - 32px)); margin:42px auto; }}
    form {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:22px; }}
    h2 {{ margin:0 0 18px; font-size:22px; }}
    .grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; }}
    label {{ display:block; font-size:13px; font-weight:700; margin-bottom:6px; }}
    input {{ width:100%; border:1px solid var(--line); border-radius:6px; padding:11px; font-size:15px; background:#fff; }}
    button {{ border:0; border-radius:6px; background:var(--accent); color:#fff; padding:11px 16px; font-weight:700; cursor:pointer; margin-top:18px; }}
    .muted {{ color:var(--muted); font-size:13px; }}
    .error {{ color:var(--accent); font-size:14px; }}
    @media (max-width:760px) {{ .grid {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <header><h1>COTAS INTELIGENTES</h1></header>
  <main>
    <form method="post" action="/login">
      <h2>Acceso de cliente</h2>
      <p class="muted">Ingrese el codigo de cliente que le fue asignado.</p>
      {note}
      <div class="grid">
        <div><label>Codigo cliente</label><input name="tenant" autocomplete="organization" required></div>
        <div><label>Usuario</label><input name="username" autocomplete="username" required></div>
        <div><label>Contrasena</label><input name="password" type="password" autocomplete="current-password" required></div>
      </div>
      <button type="submit">Entrar</button>
    </form>
  </main>
</body>
</html>""".encode("utf-8")


class TenantPortal(BaseHTTPRequestHandler):
    server_version = "CotasTenantPortal/1.0"

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def send_html(self, payload: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def current_tenant(self) -> dict | None:
        slug = valid_signed_value(get_cookie(self.headers.get("Cookie", ""), PORTAL_COOKIE))
        if not slug:
            return None
        tenant = find_tenant(slug)
        if not tenant or tenant.get("rent_status") not in ACTIVE_STATUSES:
            return None
        return tenant

    def redirect_login(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/login")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            self.send_html(login_page())
            return
        if parsed.path == "/logout":
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", f"{PORTAL_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            self.send_header("Set-Cookie", f"{APP_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            self.end_headers()
            return
        tenant = self.current_tenant()
        if not tenant:
            self.redirect_login()
            return
        self.proxy_to_tenant(tenant)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            self.handle_login()
            return
        tenant = self.current_tenant()
        if not tenant:
            self.redirect_login()
            return
        self.proxy_to_tenant(tenant)

    def handle_login(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        form = parse_qs(raw)
        slug = (form.get("tenant", [""])[0] or "").strip().lower()
        username = (form.get("username", [""])[0] or "").strip()
        password = form.get("password", [""])[0] or ""
        tenant = find_login_tenant(slug, username, password)
        if not tenant:
            self.send_html(login_page("Codigo, usuario o contrasena incorrectos."), 403)
            return
        secure = " Secure;" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/admin")
        self.send_header("Set-Cookie", f"{PORTAL_COOKIE}={sign_value(slug)}; Path=/; HttpOnly;{secure} SameSite=Lax; Max-Age=28800")
        self.send_header("Set-Cookie", f"{APP_COOKIE}={app_session(username, tenant['secret_key'])}; Path=/; HttpOnly;{secure} SameSite=Lax; Max-Age=28800")
        self.end_headers()

    def proxy_to_tenant(self, tenant: dict) -> None:
        body = b""
        if self.command in {"POST", "PUT", "PATCH"}:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length)

        headers = {}
        skip_request_headers = {"host", "connection", "content-length", "accept-encoding"}
        for key, value in self.headers.items():
            if key.lower() not in skip_request_headers:
                headers[key] = value
        headers["Host"] = self.headers.get("Host", "")
        if body:
            headers["Content-Length"] = str(len(body))

        try:
            conn = http.client.HTTPConnection("127.0.0.1", int(tenant["port"]), timeout=300)
            conn.request(self.command, self.path, body=body, headers=headers)
            response = conn.getresponse()
            payload = response.read()
        except OSError as exc:
            self.send_html(f"<h1>Tenant no disponible</h1><p>{esc(exc)}</p>".encode("utf-8"), 502)
            return

        self.send_response(response.status)
        skip_response_headers = {"connection", "transfer-encoding", "content-length", "server", "date"}
        for key, value in response.getheaders():
            if key.lower() not in skip_response_headers:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    if not AUTH_SECRET:
        raise SystemExit("Defina COTAS_PORTAL_SECRET o COTAS_SUPERADMIN_SECRET.")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8100
    httpd = ThreadingHTTPServer(("127.0.0.1", port), TenantPortal)
    print(f"COTAS Tenant Portal listo en http://127.0.0.1:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
