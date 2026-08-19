from __future__ import annotations

import html
import hmac
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
AUTH_USER = os.getenv("COTAS_SUPERADMIN_USER", "superadmin")
AUTH_PASSWORD = os.getenv("COTAS_SUPERADMIN_PASSWORD", "")
AUTH_SECRET = os.getenv("COTAS_SUPERADMIN_SECRET", "")
COOKIE_NAME = "cotas_superadmin"
STATUSES = ["activo", "prueba", "suspendido", "vencido"]


def writable_dir(configured: str, fallbacks: list[Path]) -> Path:
    candidates = [Path(configured)] if configured else []
    candidates.extend(fallbacks)
    for candidate in candidates:
        path = candidate.resolve()
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_test"
            probe.write_text("ok", encoding="ascii")
            probe.unlink(missing_ok=True)
            return path
        except OSError:
            continue
    fallback = fallbacks[-1].resolve()
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


TENANT_ROOT = writable_dir(
    os.getenv("COTAS_TENANT_ROOT", ""),
    [ROOT / "storage" / "tenants", ROOT / "runtime_storage" / "tenants"],
)
GENERATED_DIR = writable_dir(
    os.getenv("COTAS_GENERATED_DIR", ""),
    [ROOT / "deploy" / "generated", ROOT / "runtime_storage" / "generated"],
)
DB_PATH = (
    Path(os.getenv("COTAS_SUPERADMIN_DB", "")).resolve()
    if os.getenv("COTAS_SUPERADMIN_DB", "").strip()
    else writable_dir("", [ROOT / "storage", ROOT / "runtime_storage"]) / "super_admin.sqlite"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower().strip())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or secrets.token_hex(4)


def auth_configured() -> bool:
    return bool(AUTH_USER and AUTH_PASSWORD and AUTH_SECRET)


def sign_session(username: str) -> str:
    signature = hmac.new(AUTH_SECRET.encode("utf-8"), username.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{username}:{signature}"


def valid_session(value: str) -> bool:
    if not auth_configured() or ":" not in value:
        return False
    username, signature = value.split(":", 1)
    expected = sign_session(username).split(":", 1)[1]
    return hmac.compare_digest(username, AUTH_USER) and hmac.compare_digest(signature, expected)


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            create table if not exists tenants (
                id integer primary key autoincrement,
                slug text not null unique,
                company_name text not null,
                subdomain text not null,
                port integer not null unique,
                app_user text not null,
                app_password text not null,
                secret_key text not null,
                plan text not null default 'mensual',
                rent_status text not null default 'activo',
                rent_due_date text,
                notes text,
                created_at text not null,
                updated_at text not null
            )
            """
        )
        conn.execute("create index if not exists idx_tenants_status on tenants(rent_status)")


def dict_rows(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


def load_tenants() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("select * from tenants order by created_at desc").fetchall()
    return dict_rows(rows)


def find_tenant(slug: str) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("select * from tenants where slug = ?", (slug,)).fetchone()
    return dict(row) if row else None


def next_port() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("select max(port) from tenants").fetchone()
    current = int(row[0]) if row and row[0] else 8087
    return current + 1


def tenant_storage(slug: str) -> Path:
    return TENANT_ROOT / slug


def tenant_excel_config_path(slug: str) -> Path:
    return tenant_storage(slug) / "excel_config.json"


def tenant_logo_path(slug: str) -> Path:
    return tenant_storage(slug) / "logo.png"


def load_excel_config(slug: str) -> dict[str, str]:
    path = tenant_excel_config_path(slug)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items() if value is not None}


def save_excel_config(slug: str, config: dict[str, str]) -> None:
    storage = tenant_storage(slug)
    storage.mkdir(parents=True, exist_ok=True)
    tenant_excel_config_path(slug).write_text(json.dumps(config, indent=2), encoding="utf-8")


def env_text(tenant: dict) -> str:
    return "\n".join(
        [
            f"COTAS_PORT={tenant['port']}",
            f"COTAS_STORAGE={tenant_storage(tenant['slug'])}",
            f"COTAS_ADMIN_USER={tenant['app_user']}",
            f"COTAS_ADMIN_PASSWORD={tenant['app_password']}",
            f"COTAS_SECRET_KEY={tenant['secret_key']}",
            "",
        ]
    )


def nginx_text(tenant: dict) -> str:
    template = (ROOT / "deploy" / "nginx-tenant-template.conf").read_text(encoding="utf-8")
    return template.replace("__SUBDOMAIN__", tenant["subdomain"]).replace("__PORT__", str(tenant["port"]))


def command_text(tenant: dict) -> str:
    slug = tenant["slug"]
    return f"""# Provisionar tenant {slug}
sudo mkdir -p /etc/cotas-tenants
sudo cp deploy/generated/{slug}.env /etc/cotas-tenants/{slug}.env
sudo chmod 600 /etc/cotas-tenants/{slug}.env
sudo cp deploy/generated/nginx-{slug}.conf /etc/nginx/sites-available/{slug}
sudo ln -s /etc/nginx/sites-available/{slug} /etc/nginx/sites-enabled/{slug}
sudo systemctl enable --now cotas-tenant@{slug}
sudo nginx -t
sudo systemctl reload nginx

# Suspender
sudo systemctl stop cotas-tenant@{slug}
sudo systemctl disable cotas-tenant@{slug}

# Reactivar
sudo systemctl enable --now cotas-tenant@{slug}
"""


def write_tenant_files(tenant: dict) -> None:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    storage = tenant_storage(tenant["slug"])
    (storage / "uploads").mkdir(parents=True, exist_ok=True)
    (storage / "jobs").mkdir(parents=True, exist_ok=True)
    (GENERATED_DIR / f"{tenant['slug']}.env").write_text(env_text(tenant), encoding="utf-8")
    (GENERATED_DIR / f"nginx-{tenant['slug']}.conf").write_text(nginx_text(tenant), encoding="utf-8")
    (GENERATED_DIR / f"{tenant['slug']}-commands.txt").write_text(command_text(tenant), encoding="utf-8")


def delete_generated_tenant_files(slug: str) -> None:
    for filename in [f"{slug}.env", f"nginx-{slug}.conf", f"{slug}-commands.txt"]:
        (GENERATED_DIR / filename).unlink(missing_ok=True)


def layout(title: str, body: str, authenticated: bool = True) -> bytes:
    nav = ""
    if authenticated:
        nav = '<nav><a href="/">Tenants</a><a href="/tenant/new">Nuevo tenant</a><a href="/logout">Salir</a></nav>'
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>
    :root {{ --ink:#172033; --muted:#667085; --line:#d8dee9; --panel:#fff; --paper:#f4f6f8; --accent:#b42318; --dark:#1f2937; --green:#157347; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Arial, Helvetica, sans-serif; color:var(--ink); background:var(--paper); }}
    header {{ background:var(--dark); color:#fff; padding:16px 22px; display:flex; align-items:center; justify-content:space-between; gap:18px; }}
    h1 {{ margin:0; font-size:18px; }}
    nav a {{ color:#fff; text-decoration:none; margin-left:16px; font-size:14px; }}
    main {{ width:min(1120px, calc(100vw - 32px)); margin:24px auto 48px; }}
    section, form {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:18px; margin-bottom:18px; }}
    .inline {{ background:transparent; border:0; padding:0; margin:0; }}
    .grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; }}
    label {{ display:block; font-size:13px; font-weight:700; margin-bottom:6px; }}
    input, select, textarea {{ width:100%; border:1px solid var(--line); border-radius:6px; padding:10px; font-size:14px; background:#fff; }}
    textarea {{ min-height:90px; resize:vertical; }}
    button, .button {{ border:0; border-radius:6px; background:var(--accent); color:#fff; padding:10px 14px; font-weight:700; cursor:pointer; text-decoration:none; display:inline-block; font-size:14px; }}
    .secondary {{ background:#344054; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); }}
    th, td {{ padding:10px 9px; border-bottom:1px solid var(--line); text-align:left; font-size:13px; vertical-align:top; }}
    th {{ background:#eef1f5; }}
    code, pre {{ font-family:Consolas, monospace; }}
    pre {{ white-space:pre-wrap; background:#111827; color:#f9fafb; padding:14px; border-radius:8px; overflow:auto; }}
    .muted {{ color:var(--muted); font-size:13px; }}
    .pill {{ display:inline-block; border-radius:999px; padding:4px 9px; font-size:12px; font-weight:700; background:#eef1f5; }}
    .activo {{ color:var(--green); }}
    .suspendido, .vencido {{ color:var(--accent); }}
    .actions {{ display:flex; align-items:center; gap:10px; margin-top:16px; flex-wrap:wrap; }}
    @media (max-width:760px) {{ header {{ align-items:flex-start; flex-direction:column; }} nav a {{ margin-left:0; margin-right:12px; }} .grid {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <header><div><h1>COTAS Super Admin</h1><div class="muted">Control de tenants y renta</div></div>{nav}</header>
  <main>{body}</main>
</body>
</html>""".encode("utf-8")


class SuperAdminApp(BaseHTTPRequestHandler):
    server_version = "CotasSuperAdmin/1.0"

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def send_html(self, title: str, body: str, status: int = 200, authenticated: bool = True) -> None:
        payload = layout(title, body, authenticated)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def current_cookie(self) -> str:
        cookies = self.headers.get("Cookie", "")
        for part in cookies.split(";"):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            if key == COOKIE_NAME:
                return unquote(value)
        return ""

    def is_authenticated(self) -> bool:
        return valid_session(self.current_cookie())

    def require_auth(self) -> bool:
        if self.is_authenticated():
            return True
        self.redirect("/login")
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/login":
            self.show_login()
            return
        if path == "/logout":
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", f"{COOKIE_NAME}=; HttpOnly; SameSite=Lax; Max-Age=0; Path=/")
            self.end_headers()
            return
        if not self.require_auth():
            return
        if path == "/":
            self.show_dashboard()
            return
        if path == "/tenant/new":
            self.show_new_tenant()
            return
        if path.startswith("/tenant/"):
            slug = unquote(path.removeprefix("/tenant/")).strip("/")
            self.show_tenant(slug)
            return
        self.send_html("No encontrado", "<section><h2>No encontrado</h2></section>", 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/login":
            self.handle_login()
            return
        if not self.require_auth():
            return
        if path == "/tenant/new":
            self.handle_new_tenant()
            return
        if path.startswith("/tenant/") and path.endswith("/update"):
            slug = unquote(path.removeprefix("/tenant/").removesuffix("/update")).strip("/")
            self.handle_update_tenant(slug)
            return
        if path.startswith("/tenant/") and path.endswith("/excel"):
            slug = unquote(path.removeprefix("/tenant/").removesuffix("/excel")).strip("/")
            self.handle_excel_config(slug)
            return
        if path.startswith("/tenant/") and path.endswith("/delete"):
            slug = unquote(path.removeprefix("/tenant/").removesuffix("/delete")).strip("/")
            self.handle_delete_tenant(slug)
            return
        self.send_html("No encontrado", "<section><h2>No encontrado</h2></section>", 404)

    def read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        parsed = parse_qs(raw)
        return {key: values[0].strip() if values else "" for key, values in parsed.items()}

    def show_login(self, message: str = "") -> None:
        note = ""
        if not auth_configured():
            note = '<p class="muted">Login no configurado. Defina COTAS_SUPERADMIN_USER, COTAS_SUPERADMIN_PASSWORD y COTAS_SUPERADMIN_SECRET.</p>'
        if message:
            note += f"<p class='muted'>{esc(message)}</p>"
        body = f"""
<form method="post" action="/login">
  <h2>Ingreso super admin</h2>
  {note}
  <div class="grid">
    <div><label>Usuario</label><input name="username" autocomplete="username"></div>
    <div><label>Contrasena</label><input name="password" type="password" autocomplete="current-password"></div>
  </div>
  <div class="actions"><button type="submit">Entrar</button></div>
</form>
"""
        self.send_html("Login", body, authenticated=False)

    def handle_login(self) -> None:
        form = self.read_form()
        username = form.get("username", "")
        password = form.get("password", "")
        if not auth_configured():
            self.show_login("Configure las variables de entorno antes de usar el panel.")
            return
        if not (hmac.compare_digest(username, AUTH_USER) and hmac.compare_digest(password, AUTH_PASSWORD)):
            self.show_login("Credenciales incorrectas.")
            return
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"{COOKIE_NAME}={quote(sign_session(username))}; HttpOnly; SameSite=Lax; Path=/")
        self.end_headers()

    def show_dashboard(self) -> None:
        tenants = load_tenants()
        counts = {status: sum(1 for item in tenants if item["rent_status"] == status) for status in STATUSES}
        rows = []
        for tenant in tenants:
            rows.append(
                f"""<tr>
  <td><a href="/tenant/{quote(tenant['slug'])}">{esc(tenant['company_name'])}</a><br><span class="muted">{esc(tenant['slug'])}</span></td>
  <td>{esc(tenant['subdomain'])}</td>
  <td>{esc(tenant['port'])}</td>
  <td><span class="pill {esc(tenant['rent_status'])}">{esc(tenant['rent_status'])}</span></td>
  <td>{esc(tenant['plan'])}</td>
  <td>{esc(tenant['rent_due_date'])}</td>
</tr>"""
            )
        table = "".join(rows) or '<tr><td colspan="6" class="muted">No hay tenants creados.</td></tr>'
        body = f"""
<section>
  <h2>Resumen</h2>
  <p class="muted">Activos: {counts['activo']} | Prueba: {counts['prueba']} | Suspendidos: {counts['suspendido']} | Vencidos: {counts['vencido']}</p>
  <div class="actions"><a class="button" href="/tenant/new">Crear tenant</a></div>
</section>
<section>
  <h2>Tenants</h2>
  <table>
    <thead><tr><th>Empresa</th><th>Subdominio</th><th>Puerto</th><th>Estado</th><th>Plan</th><th>Vence</th></tr></thead>
    <tbody>{table}</tbody>
  </table>
</section>
"""
        self.send_html("Tenants", body)

    def show_new_tenant(self, message: str = "") -> None:
        port = next_port()
        password = secrets.token_urlsafe(10)
        secret = secrets.token_urlsafe(32)
        note = f"<p class='muted'>{esc(message)}</p>" if message else ""
        body = f"""
<form method="post" action="/tenant/new">
  <h2>Nuevo tenant</h2>
  {note}
  <div class="grid">
    <div><label>Empresa</label><input name="company_name" required></div>
    <div><label>Slug</label><input name="slug" placeholder="cliente1"></div>
    <div><label>Subdominio</label><input name="subdomain" placeholder="cliente1.tudominio.com" required></div>
    <div><label>Puerto interno</label><input name="port" type="number" value="{port}" required></div>
    <div><label>Usuario app</label><input name="app_user" value="admin" required></div>
    <div><label>Contrasena app</label><input name="app_password" value="{esc(password)}" required></div>
    <div><label>Plan</label><input name="plan" value="mensual"></div>
    <div><label>Estado renta</label>{self.status_select("activo")}</div>
    <div><label>Vence</label><input name="rent_due_date" type="date"></div>
  </div>
  <label>Secret key</label><input name="secret_key" value="{esc(secret)}" required>
  <label>Notas</label><textarea name="notes"></textarea>
  <div class="actions"><button type="submit">Crear</button><a class="button secondary" href="/">Cancelar</a></div>
</form>
"""
        self.send_html("Nuevo tenant", body)

    def status_select(self, selected: str) -> str:
        options = "".join(
            f'<option value="{esc(status)}"{" selected" if status == selected else ""}>{esc(status)}</option>'
            for status in STATUSES
        )
        return f'<select name="rent_status">{options}</select>'

    def handle_new_tenant(self) -> None:
        form = self.read_form()
        company_name = form.get("company_name", "")
        slug = slugify(form.get("slug") or company_name)
        subdomain = form.get("subdomain", "")
        try:
            port = int(form.get("port", "0"))
        except ValueError:
            port = 0
        if not company_name or not subdomain or port <= 0:
            self.show_new_tenant("Empresa, subdominio y puerto son obligatorios.")
            return
        tenant = {
            "slug": slug,
            "company_name": company_name,
            "subdomain": subdomain,
            "port": port,
            "app_user": form.get("app_user") or "admin",
            "app_password": form.get("app_password") or secrets.token_urlsafe(10),
            "secret_key": form.get("secret_key") or secrets.token_urlsafe(32),
            "plan": form.get("plan") or "mensual",
            "rent_status": form.get("rent_status") if form.get("rent_status") in STATUSES else "activo",
            "rent_due_date": form.get("rent_due_date"),
            "notes": form.get("notes"),
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    """
                    insert into tenants (
                        slug, company_name, subdomain, port, app_user, app_password, secret_key,
                        plan, rent_status, rent_due_date, notes, created_at, updated_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tenant["slug"],
                        tenant["company_name"],
                        tenant["subdomain"],
                        tenant["port"],
                        tenant["app_user"],
                        tenant["app_password"],
                        tenant["secret_key"],
                        tenant["plan"],
                        tenant["rent_status"],
                        tenant["rent_due_date"],
                        tenant["notes"],
                        tenant["created_at"],
                        tenant["updated_at"],
                    ),
                )
        except sqlite3.IntegrityError as exc:
            self.show_new_tenant(f"No se pudo crear: slug o puerto duplicado. {exc}")
            return
        write_tenant_files(tenant)
        self.redirect(f"/tenant/{quote(slug)}")

    def show_tenant(self, slug: str, message: str = "") -> None:
        tenant = find_tenant(slug)
        if not tenant:
            self.send_html("No encontrado", "<section><h2>Tenant no encontrado</h2></section>", 404)
            return
        write_tenant_files(tenant)
        excel_config = load_excel_config(tenant["slug"])
        logo_note = "Logo cargado" if tenant_logo_path(tenant["slug"]).exists() else "Sin logo personalizado"
        note = f"<p class='muted'>{esc(message)}</p>" if message else ""
        body = f"""
<section>
  <h2>{esc(tenant['company_name'])}</h2>
  {note}
  <p><strong>URL:</strong> {esc(tenant['subdomain'])}</p>
  <p><strong>Login para compartir:</strong> usuario <code>{esc(tenant['app_user'])}</code> / contrasena <code>{esc(tenant['app_password'])}</code></p>
  <p><strong>Storage:</strong> <code>{esc(tenant_storage(tenant['slug']))}</code></p>
</section>
<form method="post" action="/tenant/{quote(tenant['slug'])}/update">
  <h2>Renta</h2>
  <div class="grid">
    <div><label>Estado</label>{self.status_select(tenant['rent_status'])}</div>
    <div><label>Plan</label><input name="plan" value="{esc(tenant['plan'])}"></div>
    <div><label>Vence</label><input name="rent_due_date" type="date" value="{esc(tenant['rent_due_date'])}"></div>
  </div>
  <label>Notas</label><textarea name="notes">{esc(tenant['notes'])}</textarea>
  <div class="actions"><button type="submit">Guardar renta</button><a class="button secondary" href="/">Volver</a></div>
</form>
<form method="post" action="/tenant/{quote(tenant['slug'])}/excel" enctype="multipart/form-data">
  <h2>Personalizacion Excel</h2>
  <p class="muted">Estos datos solo afectan los Excel nuevos o los que se regeneren con Guardar PDF/Excel y volver.</p>
  <div class="grid">
    <div><label>Nombre de compania</label><input name="company_name" value="{esc(excel_config.get('company_name') or tenant['company_name'])}"></div>
    <div><label>Titulo del Excel</label><input name="document_title" value="{esc(excel_config.get('document_title') or 'INSPECCION FINAL DE PRODUCTO')}"></div>
    <div><label>Codigo / revision</label><input name="document_code" value="{esc(excel_config.get('document_code') or 'SR-P-19-02 Rev 04 Emision: 30/07/25')}"></div>
  </div>
  <label>Logo PNG/JPG</label><input name="logo" type="file" accept=".png,.jpg,.jpeg,image/png,image/jpeg">
  <p class="muted">{esc(logo_note)}. El logo se guarda en <code>{esc(tenant_logo_path(tenant['slug']))}</code>.</p>
  <div class="actions"><button type="submit">Guardar personalizacion</button></div>
</form>
<section>
  <h2>Archivos generados</h2>
  <p class="muted">Estos archivos se copiaron en <code>deploy/generated</code>. No se suben a git.</p>
  <h3>ENV</h3>
  <pre>{esc(env_text(tenant))}</pre>
  <h3>Nginx</h3>
  <pre>{esc(nginx_text(tenant))}</pre>
  <h3>Comandos VPS</h3>
  <pre>{esc(command_text(tenant))}</pre>
</section>
<form method="post" action="/tenant/{quote(tenant['slug'])}/delete" onsubmit="return confirm('Eliminar este tenant del panel? Los archivos del cliente no se borran automaticamente.');">
  <h2>Eliminar tenant</h2>
  <p class="muted">Esto elimina el tenant del panel y borra sus archivos generados. No borra PDFs, historial ni apaga el servicio en el VPS.</p>
  <p class="muted">Antes de eliminar un tenant ya activado, puede suspenderlo en el VPS con <code>sudo systemctl disable --now cotas-tenant@{esc(tenant['slug'])}</code>.</p>
  <label>Escriba el slug para confirmar: <code>{esc(tenant['slug'])}</code></label>
  <input name="confirm_slug" autocomplete="off">
  <div class="actions"><button type="submit">Eliminar tenant</button></div>
</form>
"""
        self.send_html(tenant["company_name"], body)

    def handle_update_tenant(self, slug: str) -> None:
        tenant = find_tenant(slug)
        if not tenant:
            self.send_html("No encontrado", "<section><h2>Tenant no encontrado</h2></section>", 404)
            return
        form = self.read_form()
        rent_status = form.get("rent_status") if form.get("rent_status") in STATUSES else tenant["rent_status"]
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                update tenants
                set rent_status = ?, plan = ?, rent_due_date = ?, notes = ?, updated_at = ?
                where slug = ?
                """,
                (
                    rent_status,
                    form.get("plan") or "mensual",
                    form.get("rent_due_date"),
                    form.get("notes"),
                    now_iso(),
                    slug,
                ),
            )
        updated = find_tenant(slug)
        if updated:
            write_tenant_files(updated)
        self.redirect(f"/tenant/{quote(slug)}")

    def handle_excel_config(self, slug: str) -> None:
        import cgi

        tenant = find_tenant(slug)
        if not tenant:
            self.send_html("No encontrado", "<section><h2>Tenant no encontrado</h2></section>", 404)
            return

        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
        current = load_excel_config(slug)
        config = {
            "company_name": (form.getfirst("company_name", "") or "").strip(),
            "document_title": (form.getfirst("document_title", "") or "").strip() or "INSPECCION FINAL DE PRODUCTO",
            "document_code": (form.getfirst("document_code", "") or "").strip(),
        }
        if current.get("logo_path"):
            config["logo_path"] = current["logo_path"]

        logo_item = form["logo"] if "logo" in form else None
        if logo_item is not None and getattr(logo_item, "filename", ""):
            filename = str(getattr(logo_item, "filename", "")).lower()
            if not filename.endswith((".png", ".jpg", ".jpeg")):
                self.show_tenant(slug, "El logo debe ser PNG o JPG.")
                return
            storage = tenant_storage(slug)
            storage.mkdir(parents=True, exist_ok=True)
            logo_path = tenant_logo_path(slug)
            with logo_path.open("wb") as handle:
                handle.write(logo_item.file.read())
            config["logo_path"] = str(logo_path)

        save_excel_config(slug, config)
        self.show_tenant(slug, "Personalizacion de Excel guardada.")

    def handle_delete_tenant(self, slug: str) -> None:
        tenant = find_tenant(slug)
        if not tenant:
            self.send_html("No encontrado", "<section><h2>Tenant no encontrado</h2></section>", 404)
            return
        form = self.read_form()
        if form.get("confirm_slug") != slug:
            self.show_tenant(slug, "Para eliminar, escriba el slug exacto del tenant.")
            return
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("delete from tenants where slug = ?", (slug,))
        delete_generated_tenant_files(slug)
        self.redirect("/")


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    init_db()
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    TENANT_ROOT.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), SuperAdminApp)
    print(f"COTAS Super Admin listo en http://127.0.0.1:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
