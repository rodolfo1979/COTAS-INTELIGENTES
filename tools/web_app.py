from __future__ import annotations

import html
import hmac
import hashlib
import json
import os
import shutil
import sys
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from cotas_engine import DimensionCandidate, analyze_command, build_parser, draw_numbered_overlay, generate_tolerance_workbook, init_history


ROOT = Path(__file__).resolve().parents[1]
STORAGE = ROOT / "storage"
UPLOADS = STORAGE / "uploads"
CLIENTS_FILE = ROOT / "data" / "clients.json"
ENGINE_VERSION = "2026-08-05-inspection-logo"
AUTH_USER = os.getenv("COTAS_ADMIN_USER", "admin")
AUTH_PASSWORD = os.getenv("COTAS_ADMIN_PASSWORD", "")
AUTH_SECRET = os.getenv("COTAS_SECRET_KEY", "")
COOKIE_NAME = "cotas_session"


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def load_clients() -> list[str]:
    fallback = ["SAMTEC", "JOHNSON & JOHNSON", "JOHNSON & JOHNSON MEDTECH CR LIMITADA"]
    if not CLIENTS_FILE.exists():
        return fallback

    try:
        clients = json.loads(CLIENTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return fallback

    seen: set[str] = set()
    ordered: list[str] = []
    for name in ["SAMTEC", *clients]:
        clean = str(name).strip()
        key = clean.upper()
        if clean and key not in seen:
            seen.add(key)
            ordered.append(clean)
    return ordered


def job_title(job: dict) -> str:
    pieces = []
    if job.get("part_number"):
        pieces.append(str(job["part_number"]))
    if job.get("drawing_number"):
        pieces.append(str(job["drawing_number"]))

    title = " / ".join(pieces) if pieces else str(job.get("id", "Trabajo"))
    if job.get("revision"):
        title += f" rev {job['revision']}"
    return title


def display_client(value: object) -> str:
    client = str(value or "Sin cliente").strip()
    return client.upper() if client else "Sin cliente"


def display_date(value: object) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else text


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


def layout(title: str, body: str, authenticated: bool = True) -> bytes:
    nav = """
      <a href="/new">Nuevo plano</a>
      <a href="/history">Historial</a>
      <a href="/admin">Admin</a>
      <a href="/logout">Salir</a>
""" if authenticated else ""
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>
    :root {{
      --ink: #172033;
      --muted: #5c667a;
      --line: #d8dee9;
      --panel: #ffffff;
      --paper: #f4f6f8;
      --accent: #c81e1e;
      --accent-dark: #991b1b;
      --green: #157347;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Arial, Helvetica, sans-serif; color: var(--ink); background: var(--paper); }}
    header {{ background: #1f2937; color: #fff; padding: 16px 22px; display: flex; justify-content: space-between; align-items: center; gap: 20px; }}
    header h1 {{ font-size: 18px; margin: 0; letter-spacing: 0; }}
    .version {{ font-size: 11px; color: #cbd5e1; margin-top: 4px; }}
    nav a {{ color: #fff; text-decoration: none; margin-left: 16px; font-size: 14px; }}
    main {{ width: min(1120px, calc(100vw - 32px)); margin: 24px auto 48px; }}
    section, form {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px; margin-bottom: 18px; }}
    .inline-form {{ background: transparent; border: 0; border-radius: 0; padding: 0; margin: 0; }}
    h2 {{ font-size: 18px; margin: 0 0 14px; }}
    label {{ display: block; font-size: 13px; font-weight: 700; margin-bottom: 6px; }}
    input, select {{ width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 10px; font-size: 14px; background: #fff; }}
    .combo {{ position: relative; }}
    .combo-menu {{ display: none; position: absolute; z-index: 30; top: calc(100% + 4px); left: 0; right: 0; max-height: 260px; overflow: auto; background: #fff; border: 1px solid var(--line); border-radius: 6px; box-shadow: 0 10px 24px rgba(15, 23, 42, 0.18); }}
    .combo-menu.open {{ display: block; }}
    .combo-option {{ width: 100%; border: 0; border-radius: 0; background: #fff; color: var(--ink); display: block; text-align: left; padding: 9px 10px; font-size: 13px; font-weight: 400; }}
    .combo-option:hover, .combo-option:focus {{ background: #eef1f5; color: var(--ink); }}
    .combo-empty {{ padding: 10px; color: var(--muted); font-size: 13px; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }}
    .stats {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-bottom: 18px; }}
    .metric {{ background: #fff; border: 1px solid var(--line); border-radius: 8px; padding: 16px; }}
    .metric strong {{ display: block; font-size: 28px; line-height: 1; margin-bottom: 6px; }}
    .bar-row {{ display: grid; grid-template-columns: 220px 1fr 46px; align-items: center; gap: 10px; margin: 10px 0; font-size: 13px; }}
    .bar-track {{ height: 12px; background: #eef1f5; border-radius: 999px; overflow: hidden; }}
    .bar-fill {{ height: 100%; background: var(--accent); }}
    .pager {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 14px; flex-wrap: wrap; }}
    .pager-links {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }}
    .page-link {{ border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; text-decoration: none; color: var(--ink); background: #fff; font-size: 13px; }}
    .page-link.active {{ background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 700; }}
    .page-link.disabled {{ color: var(--muted); pointer-events: none; background: #eef1f5; }}
    .danger {{ background: #b42318; }}
    .danger:hover {{ background: #8f1d14; }}
    .wide {{ grid-column: span 2; }}
    .actions {{ display: flex; align-items: center; gap: 10px; margin-top: 16px; }}
    button, .button {{ border: 0; border-radius: 6px; background: var(--accent); color: #fff; padding: 10px 14px; font-weight: 700; cursor: pointer; text-decoration: none; display: inline-block; font-size: 14px; }}
    button:hover, .button:hover {{ background: var(--accent-dark); }}
    .secondary {{ background: #344054; }}
    .secondary:hover {{ background: #1f2937; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid var(--line); }}
    th, td {{ padding: 10px 9px; border-bottom: 1px solid var(--line); text-align: left; font-size: 13px; vertical-align: top; }}
    th {{ background: #eef1f5; color: #283142; }}
    .muted {{ color: var(--muted); font-size: 13px; }}
    .ok {{ color: var(--green); font-weight: 700; }}
    iframe {{ width: 100%; height: 760px; border: 1px solid var(--line); border-radius: 8px; background: #fff; }}
    @media (max-width: 820px) {{
      .grid {{ grid-template-columns: 1fr; }}
      .stats {{ grid-template-columns: 1fr; }}
      .bar-row {{ grid-template-columns: 1fr; }}
      .wide {{ grid-column: span 1; }}
      header {{ align-items: flex-start; flex-direction: column; }}
      nav a {{ margin: 0 12px 0 0; }}
    }}
  </style>
</head>
<body>
  <header>
    <div><h1>Planos Cotas</h1><div class="version">Motor {esc(ENGINE_VERSION)}</div></div>
    <nav>
{nav}
    </nav>
  </header>
  <main>{body}</main>
</body>
</html>""".encode("utf-8")


def run_engine(input_pdf: Path, fields: dict[str, str]) -> dict:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--storage",
            str(STORAGE),
            "analyze",
            str(input_pdf),
            "--client",
            fields.get("client", ""),
            "--part-number",
            fields.get("part_number", ""),
            "--drawing-number",
            fields.get("drawing_number", ""),
            "--revision",
            fields.get("revision", ""),
        ]
    )
    analyze_command(args)

    # The engine prints JSON for CLI usage; the web app reads the newest matching job.
    parser = build_parser()
    search_args = parser.parse_args(
        [
            "--storage",
            str(STORAGE),
            "search",
            "--client",
            fields.get("client", ""),
            "--part-number",
            fields.get("part_number", ""),
            "--drawing-number",
            fields.get("drawing_number", ""),
            "--revision",
            fields.get("revision", ""),
            "--limit",
            "1",
        ]
    )
    db_path = STORAGE / "history.sqlite"
    init_history(db_path)
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            select * from jobs
            where client = ? and part_number = ? and drawing_number = ? and revision = ?
            order by created_at desc limit 1
            """,
            (
                fields.get("client", ""),
                fields.get("part_number", ""),
                fields.get("drawing_number", ""),
                fields.get("revision", ""),
            ),
        ).fetchone()
    if not row:
        raise RuntimeError("No se pudo leer el trabajo generado.")
    return dict(row)


class App(BaseHTTPRequestHandler):
    server_version = f"PlanosCotas/{ENGINE_VERSION}"

    def send_html(self, title: str, body: str, status: int = 200, authenticated: bool = True) -> None:
        payload = layout(title, body, authenticated=authenticated)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def read_cookie(self, name: str) -> str:
        header = self.headers.get("Cookie", "")
        for part in header.split(";"):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            if key == name:
                return value
        return ""

    def is_authenticated(self) -> bool:
        return valid_session(self.read_cookie(COOKIE_NAME))

    def require_auth(self, parsed_path: str) -> bool:
        if parsed_path in {"/login", "/version"}:
            return True
        if self.is_authenticated():
            return True
        target = quote(self.path, safe="")
        self.redirect(f"/login?next={target}")
        return False

    def set_session_cookie(self, value: str) -> None:
        secure = " Secure;" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}={value}; Path=/; HttpOnly;{secure} SameSite=Lax; Max-Age=28800",
        )

    def clear_session_cookie(self) -> None:
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
        )

    def send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if not self.require_auth(parsed.path):
            return
        if parsed.path == "/login":
            self.show_login(parsed.query)
        elif parsed.path == "/logout":
            self.handle_logout()
        elif parsed.path == "/":
            self.redirect("/admin")
        elif parsed.path == "/new":
            self.show_home()
        elif parsed.path == "/version":
            self.send_json({"engine_version": ENGINE_VERSION, "root": str(ROOT)})
        elif parsed.path == "/history":
            self.show_history(parsed.query)
        elif parsed.path == "/admin":
            self.show_admin(parsed.query)
        elif parsed.path.startswith("/job/"):
            self.show_job(unquote(parsed.path.removeprefix("/job/")))
        elif parsed.path.startswith("/edit/"):
            self.show_edit(unquote(parsed.path.removeprefix("/edit/")))
        elif parsed.path.startswith("/view/"):
            parts = parsed.path.removeprefix("/view/").split("/", 1)
            if len(parts) == 2:
                self.show_file_view(unquote(parts[0]), unquote(parts[1]))
            else:
                self.send_html("No encontrado", "<section><h2>No encontrado</h2></section>", 404)
        elif parsed.path.startswith("/file/"):
            self.send_file(unquote(parsed.path.removeprefix("/file/")))
        else:
            self.send_html("No encontrado", "<section><h2>No encontrado</h2></section>", 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not self.require_auth(parsed.path):
            return
        if parsed.path == "/login":
            self.handle_login()
        elif parsed.path == "/upload":
            self.handle_upload()
        elif parsed.path.startswith("/edit/"):
            self.handle_edit(unquote(parsed.path.removeprefix("/edit/")))
        elif parsed.path.startswith("/delete/"):
            self.handle_delete(unquote(parsed.path.removeprefix("/delete/")))
        else:
            self.send_html("No encontrado", "<section><h2>No encontrado</h2></section>", 404)

    def show_login(self, query: str = "", error: str = "") -> None:
        params = parse_qs(query)
        next_url = params.get("next", ["/admin"])[0] or "/admin"
        if not next_url.startswith("/"):
            next_url = "/admin"
        message = f'<p class="muted">{esc(error)}</p>' if error else ""
        if not auth_configured():
            message = '<p class="muted">Login no configurado. Defina COTAS_ADMIN_USER, COTAS_ADMIN_PASSWORD y COTAS_SECRET_KEY en el servidor.</p>'
        disabled = "disabled" if not auth_configured() else ""
        body = f"""
<form method="post" action="/login">
  <h2>Acceso al sistema</h2>
  {message}
  <input type="hidden" name="next" value="{esc(next_url)}">
  <div class="grid">
    <div class="wide"><label>Usuario</label><input name="username" autocomplete="username" required {disabled}></div>
    <div class="wide"><label>Contrasena</label><input type="password" name="password" autocomplete="current-password" required {disabled}></div>
  </div>
  <div class="actions"><button type="submit" {disabled}>Entrar</button></div>
</form>
"""
        self.send_html("Login", body, authenticated=False)

    def handle_login(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        form = parse_qs(raw)
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        next_url = form.get("next", ["/admin"])[0] or "/admin"
        if not next_url.startswith("/"):
            next_url = "/admin"
        if not auth_configured():
            self.show_login(urlencode({"next": next_url}), "Login no configurado en el servidor.")
            return
        if not (hmac.compare_digest(username, AUTH_USER) and hmac.compare_digest(password, AUTH_PASSWORD)):
            self.show_login(urlencode({"next": next_url}), "Usuario o contrasena incorrectos.")
            return
        self.send_response(HTTPStatus.SEE_OTHER)
        self.set_session_cookie(sign_session(username))
        self.send_header("Location", next_url)
        self.end_headers()

    def handle_logout(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.clear_session_cookie()
        self.send_header("Location", "/login")
        self.end_headers()

    def show_home(self) -> None:
        clients_json = json.dumps(load_clients())
        body = """
<form method="post" action="/upload" enctype="multipart/form-data">
  <h2>Subir plano para numerar cotas</h2>
  <div class="grid">
    <div>
      <label>Cliente</label>
      <div class="combo">
        <input id="client-input" name="client" value="SAMTEC" autocomplete="off" required>
        <div id="client-menu" class="combo-menu" role="listbox"></div>
      </div>
    </div>
    <div><label>Numero de parte</label><input name="part_number"></div>
    <div><label>Numero de plano</label><input name="drawing_number" required></div>
    <div><label>Revision</label><input name="revision" value="A"></div>
    <div class="wide"><label>PDF del plano</label><input type="file" name="pdf" accept="application/pdf" required></div>
  </div>
  <div class="actions"><button type="submit">Analizar y numerar</button><span class="muted">El sistema guardara original, numerado e historial.</span></div>
</form>
<section>
  <h2>Como trabaja esta version</h2>
  <p class="muted">El cliente se puede buscar en la lista o escribir manualmente si no existe. Solo cliente y numero de plano son obligatorios; numero de parte y revision quedan como datos opcionales.</p>
  <p class="muted">Detecta textos que parecen cotas en PDFs vectoriales y excluye automaticamente numeros dentro de tablas o cajetines. Si el plano viene escaneado como imagen, intenta usar OCR automatico.</p>
</section>
<script>
  const clientNames = CLIENTS_JSON;
  const clientInput = document.getElementById("client-input");
  const clientMenu = document.getElementById("client-menu");
  let clientTouched = false;

  function renderClients(showAll = false) {
    const term = clientInput.value.trim().toLowerCase();
    const matches = clientNames.filter((name) => showAll || name.toLowerCase().includes(term));
    clientMenu.innerHTML = "";

    if (!matches.length) {
      const empty = document.createElement("div");
      empty.className = "combo-empty";
      empty.textContent = "No esta en la lista; puede escribirlo manualmente.";
      clientMenu.appendChild(empty);
    }

    matches.slice(0, 80).forEach((name) => {
      const option = document.createElement("button");
      option.type = "button";
      option.className = "combo-option";
      option.textContent = name;
      option.addEventListener("mousedown", (event) => {
        event.preventDefault();
        clientInput.value = name;
        clientMenu.classList.remove("open");
      });
      clientMenu.appendChild(option);
    });

    clientMenu.classList.add("open");
  }

  clientInput.addEventListener("focus", () => renderClients(!clientTouched || clientInput.value === "SAMTEC"));
  clientInput.addEventListener("input", () => {
    clientTouched = true;
    renderClients(false);
  });
  document.addEventListener("mousedown", (event) => {
    if (!clientMenu.contains(event.target) && event.target !== clientInput) {
      clientMenu.classList.remove("open");
    }
  });
</script>
""".replace("CLIENTS_JSON", clients_json)
        self.send_html("Nuevo plano", body)

    def show_history(self, query: str) -> None:
        import sqlite3

        params = parse_qs(query)
        q = {key: params.get(key, [""])[0] for key in ["client", "part_number", "drawing_number", "revision"]}
        db_path = STORAGE / "history.sqlite"
        init_history(db_path)

        clauses = []
        values = []
        for key, value in q.items():
            if value:
                clauses.append(f"{key} like ?")
                values.append(f"%{value}%")
        sql = "select * from jobs"
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by created_at desc limit 100"

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute(sql, values)]

        filters = f"""
<form method="get" action="/history">
  <h2>Buscar historial</h2>
  <div class="grid">
    <div><label>Cliente</label><input name="client" value="{esc(q['client'])}"></div>
    <div><label>Parte</label><input name="part_number" value="{esc(q['part_number'])}"></div>
    <div><label>Plano</label><input name="drawing_number" value="{esc(q['drawing_number'])}"></div>
    <div><label>Revision</label><input name="revision" value="{esc(q['revision'])}"></div>
  </div>
  <div class="actions"><button type="submit" class="secondary">Buscar</button></div>
</form>
"""
        table_rows = "".join(
            f"""<tr>
  <td><a href="/job/{quote(row['id'])}">{esc(row['id'])}</a></td>
  <td>{esc(row['client'])}</td>
  <td>{esc(row['part_number'])}</td>
  <td>{esc(row['drawing_number'])}</td>
  <td>{esc(row['revision'])}</td>
  <td>{esc(display_date(row['created_at']))}</td>
</tr>"""
            for row in rows
        )
        if not table_rows:
            table_rows = '<tr><td colspan="6" class="muted">No hay registros todavia.</td></tr>'
        body = filters + f"""
<section>
  <h2>Trabajos guardados</h2>
  <table>
    <thead><tr><th>ID</th><th>Cliente</th><th>Parte</th><th>Plano</th><th>Revision</th><th>Fecha</th></tr></thead>
    <tbody>{table_rows}</tbody>
  </table>
</section>
"""
        self.send_html("Historial", body)

    def show_admin(self, query: str = "") -> None:
        jobs = self.list_jobs(limit=500)
        total = len(jobs)
        clients = sorted({display_client(job.get("client")) for job in jobs})
        total_cotas = sum(self.candidate_count(job) for job in jobs)
        latest = jobs[0]["created_at"] if jobs else "Sin registros"

        counts_by_client: dict[str, int] = {}
        for job in jobs:
            client = display_client(job.get("client"))
            counts_by_client[client] = counts_by_client.get(client, 0) + 1

        top_clients = sorted(counts_by_client.items(), key=lambda item: item[1], reverse=True)[:10]
        max_count = max([count for _, count in top_clients], default=1)
        bars = "".join(
            f"""<div class="bar-row">
  <div>{esc(client)}</div>
  <div class="bar-track"><div class="bar-fill" style="width: {max(6, int(count / max_count * 100))}%"></div></div>
  <div>{count}</div>
</div>"""
            for client, count in top_clients
        )
        if not bars:
            bars = '<p class="muted">Todavia no hay datos para graficar.</p>'

        params = parse_qs(query)
        per_page = 10
        total_pages = max(1, (total + per_page - 1) // per_page)
        try:
            current_page = int(params.get("page", ["1"])[0])
        except ValueError:
            current_page = 1
        current_page = max(1, min(current_page, total_pages))
        start = (current_page - 1) * per_page
        end = start + per_page
        page_jobs = jobs[start:end]

        rows = "".join(
            f"""<tr>
  <td><a href="/job/{quote(job['id'])}">{esc(job.get('drawing_number') or job['id'])}</a></td>
  <td>{esc(job.get('client'))}</td>
  <td>{esc(job.get('part_number'))}</td>
  <td>{esc(job.get('revision'))}</td>
  <td>{self.candidate_count(job)}</td>
  <td>{esc(display_date(job.get('created_at')))}</td>
  <td>
    <form class="inline-form" method="post" action="/delete/{quote(job['id'])}?page={current_page}" onsubmit="return confirm('Eliminar este plano y sus archivos?');">
      <button class="danger" type="submit">Eliminar</button>
    </form>
  </td>
</tr>"""
            for job in page_jobs
        )
        if not rows:
            rows = '<tr><td colspan="7" class="muted">No hay planos cargados.</td></tr>'

        page_links = []
        previous_class = "page-link" if current_page > 1 else "page-link disabled"
        next_class = "page-link" if current_page < total_pages else "page-link disabled"
        page_links.append(f'<a class="{previous_class}" href="/admin?page={current_page - 1}">Anterior</a>')
        for page_number in range(1, total_pages + 1):
            if total_pages > 9 and page_number not in {1, total_pages, current_page - 1, current_page, current_page + 1}:
                if page_links[-1] != '<span class="muted">...</span>':
                    page_links.append('<span class="muted">...</span>')
                continue
            active = " active" if page_number == current_page else ""
            page_links.append(f'<a class="page-link{active}" href="/admin?page={page_number}">{page_number}</a>')
        page_links.append(f'<a class="{next_class}" href="/admin?page={current_page + 1}">Siguiente</a>')
        pager = f"""
  <div class="pager">
    <span class="muted">Mostrando {start + 1 if total else 0}-{min(end, total)} de {total} planos</span>
    <div class="pager-links">{''.join(page_links)}</div>
  </div>
"""

        body = f"""
<div class="stats">
  <div class="metric"><strong>{total}</strong><span class="muted">Planos cargados</span></div>
  <div class="metric"><strong>{len(clients)}</strong><span class="muted">Clientes</span></div>
  <div class="metric"><strong>{total_cotas}</strong><span class="muted">Cotas propuestas</span></div>
  <div class="metric"><strong>{esc(latest[:10])}</strong><span class="muted">Ultima carga</span></div>
</div>
<section>
  <h2>Administracion</h2>
  <div class="actions">
    <a class="button" href="/new">Cargar plano nuevo</a>
    <a class="button secondary" href="/history">Buscar historial</a>
  </div>
</section>
<section>
  <h2>Planos por cliente</h2>
  {bars}
</section>
<section>
  <h2>Historial administrativo</h2>
  <table>
    <thead><tr><th>Plano</th><th>Cliente</th><th>Parte</th><th>Revision</th><th>Cotas</th><th>Fecha</th><th>Accion</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  {pager}
</section>
"""
        self.send_html("Admin", body)

    def show_job(self, job_id: str) -> None:
        import sqlite3

        db_path = STORAGE / "history.sqlite"
        init_history(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select * from jobs where id = ?", (job_id,)).fetchone()
        if not row:
            self.send_html("No encontrado", "<section><h2>Trabajo no encontrado</h2></section>", 404)
            return
        job = dict(row)
        candidates = json.loads(Path(job["candidates_json"]).read_text(encoding="utf-8"))
        tolerances_xlsx = Path(job["numbered_pdf"]).with_name("tolerancias.xlsx")
        tolerance_button = (
            f'<a class="button secondary" href="/file/{quote(str(tolerances_xlsx))}">Descargar Excel tolerancias</a>'
            if tolerances_xlsx.exists()
            else ""
        )
        candidate_rows = "".join(
            f"<tr><td>{esc(c['number'])}</td><td>{esc(c['page'])}</td><td>{esc(c['text'])}</td><td>{esc(c['confidence'])}</td><td>{esc(c['reason'])}</td></tr>"
            for c in candidates
        )
        body = f"""
<section>
  <h2>{esc(job_title(job))}</h2>
  <p class="ok">{len(candidates)} cotas propuestas</p>
  <div class="actions">
    <a class="button" href="/file/{quote(job['numbered_pdf'])}">Descargar PDF numerado</a>
    {tolerance_button}
    <a class="button secondary" href="/edit/{quote(job['id'])}">Revisar cotas</a>
    <a class="button secondary" href="/view/{quote(job['id'])}/original">Ver original</a>
    <a class="button secondary" href="/view/{quote(job['id'])}/json">Ver JSON</a>
  </div>
</section>
<section>
  <h2>Vista del PDF numerado</h2>
  <iframe src="/file/{quote(job['numbered_pdf'])}"></iframe>
</section>
<section>
  <h2>Cotas detectadas</h2>
  <table>
    <thead><tr><th>#</th><th>Pagina</th><th>Texto</th><th>Confianza</th><th>Motivo</th></tr></thead>
    <tbody>{candidate_rows}</tbody>
  </table>
</section>
"""
        self.send_html("Trabajo", body)

    def show_file_view(self, job_id: str, kind: str) -> None:
        job = self.find_job(job_id)
        if not job:
            self.send_html("No encontrado", "<section><h2>Trabajo no encontrado</h2></section>", 404)
            return

        files = {
            "original": ("Plano original", job["original_pdf"]),
            "numbered": ("PDF numerado", job["numbered_pdf"]),
            "json": ("Cotas detectadas JSON", job["candidates_json"]),
            "tolerances": ("Excel de tolerancias", str(Path(job["numbered_pdf"]).with_name("tolerancias.xlsx"))),
        }
        if kind not in files:
            self.send_html("No encontrado", "<section><h2>Archivo no encontrado</h2></section>", 404)
            return

        title, path = files[kind]
        viewer = (
            f'<iframe src="/file/{quote(path)}"></iframe>'
            if kind != "json"
            else f'<iframe src="/file/{quote(path)}"></iframe>'
        )
        body = f"""
<section>
  <h2>{esc(title)} - {esc(job_title(job))}</h2>
  <div class="actions">
    <a class="button secondary" href="/job/{quote(job['id'])}">Volver al trabajo</a>
    <a class="button" href="/file/{quote(path)}">Descargar</a>
  </div>
</section>
<section>
  {viewer}
</section>
"""
        self.send_html(title, body)

    def show_edit(self, job_id: str) -> None:
        job = self.find_job(job_id)
        if not job:
            self.send_html("No encontrado", "<section><h2>Trabajo no encontrado</h2></section>", 404)
            return
        candidates = json.loads(Path(job["candidates_json"]).read_text(encoding="utf-8"))
        rows = "".join(
            f"""<tr>
  <td><input type="checkbox" name="keep_{index}" checked></td>
  <td><input name="number_{index}" value="{esc(c['number'])}"></td>
  <td><input name="page_{index}" value="{esc(c['page'])}"></td>
  <td><input name="text_{index}" value="{esc(c.get('text', ''))}"></td>
  <td><input name="x_{index}" value="{esc(c['x'])}"></td>
  <td><input name="y_{index}" value="{esc(c['y'])}"></td>
  <td><input name="width_{index}" value="{esc(c.get('width', 0))}"></td>
  <td><input name="height_{index}" value="{esc(c.get('height', 0))}"></td>
</tr>"""
            for index, c in enumerate(candidates)
        )
        body = f"""
<form method="post" action="/edit/{quote(job['id'])}">
  <h2>Revisar cotas propuestas</h2>
  <p class="muted">Desmarque una fila para eliminarla. Cambie los numeros si necesita otro orden. Las coordenadas permiten mover el globo cuando haga falta.</p>
  <input type="hidden" name="count" value="{len(candidates)}">
  <table>
    <thead><tr><th>Usar</th><th>#</th><th>Pag.</th><th>Texto</th><th>X</th><th>Y</th><th>Ancho</th><th>Alto</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <div class="actions">
    <button type="submit">Guardar y regenerar PDF</button>
    <a class="button secondary" href="/job/{quote(job['id'])}">Volver</a>
  </div>
</form>
"""
        self.send_html("Revisar cotas", body)

    def handle_edit(self, job_id: str) -> None:
        import cgi

        job = self.find_job(job_id)
        if not job:
            self.send_html("No encontrado", "<section><h2>Trabajo no encontrado</h2></section>", 404)
            return
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
        count = int(form.getfirst("count", "0") or "0")
        candidates: list[DimensionCandidate] = []
        for index in range(count):
            if form.getfirst(f"keep_{index}") != "on":
                continue
            candidates.append(
                DimensionCandidate(
                    number=int(float(form.getfirst(f"number_{index}", "0") or "0")),
                    page=int(float(form.getfirst(f"page_{index}", "1") or "1")),
                    text=form.getfirst(f"text_{index}", "") or "",
                    x=float(form.getfirst(f"x_{index}", "0") or "0"),
                    y=float(form.getfirst(f"y_{index}", "0") or "0"),
                    width=float(form.getfirst(f"width_{index}", "0") or "0"),
                    height=float(form.getfirst(f"height_{index}", "0") or "0"),
                    confidence=1.0,
                    reason="manual review",
                )
            )
        candidates.sort(key=lambda item: (item.page, item.number))
        candidates_json = Path(job["candidates_json"])
        candidates_json.write_text(
            json.dumps([candidate.__dict__ for candidate in candidates], indent=2),
            encoding="utf-8",
        )
        draw_numbered_overlay(Path(job["original_pdf"]), candidates, Path(job["numbered_pdf"]))
        generate_tolerance_workbook(candidates, Path(job["numbered_pdf"]).with_name("tolerancias.xlsx"), job)
        self.redirect(f"/job/{quote(job_id)}")

    def find_job(self, job_id: str) -> dict | None:
        import sqlite3

        db_path = STORAGE / "history.sqlite"
        init_history(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select * from jobs where id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 100) -> list[dict]:
        import sqlite3

        db_path = STORAGE / "history.sqlite"
        init_history(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "select * from jobs order by created_at desc limit ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def candidate_count(self, job: dict) -> int:
        try:
            path = Path(job["candidates_json"])
            return len(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return 0

    def handle_delete(self, job_id: str) -> None:
        import sqlite3

        page = parse_qs(urlparse(self.path).query).get("page", ["1"])[0]
        job = self.find_job(job_id)
        if not job:
            self.redirect(f"/admin?page={quote(page)}")
            return

        job_dir = Path(job["original_pdf"]).resolve().parent
        jobs_root = (STORAGE / "jobs").resolve()
        try:
            job_dir.relative_to(jobs_root)
        except ValueError:
            self.send_html("Error", "<section><h2>No se pudo validar la carpeta del plano.</h2></section>", 400)
            return

        shutil.rmtree(job_dir, ignore_errors=True)
        db_path = STORAGE / "history.sqlite"
        with sqlite3.connect(db_path) as conn:
            conn.execute("delete from jobs where id = ?", (job_id,))

        self.redirect(f"/admin?page={quote(page)}")

    def handle_upload(self) -> None:
        import cgi

        UPLOADS.mkdir(parents=True, exist_ok=True)
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
        fields = {
            "client": form.getfirst("client", "").strip(),
            "part_number": form.getfirst("part_number", "").strip(),
            "drawing_number": form.getfirst("drawing_number", "").strip(),
            "revision": form.getfirst("revision", "").strip(),
        }
        if not fields["client"] or not fields["drawing_number"]:
            self.send_html(
                "Error",
                "<section><h2>Cliente y numero de plano son obligatorios.</h2></section>",
                400,
            )
            return
        file_item = form["pdf"] if "pdf" in form else None
        if file_item is None or not getattr(file_item, "filename", ""):
            self.send_html("Error", "<section><h2>Debe subir un PDF.</h2></section>", 400)
            return
        upload_path = UPLOADS / f"{uuid.uuid4().hex}.pdf"
        with upload_path.open("wb") as handle:
            shutil.copyfileobj(file_item.file, handle)

        try:
            job = run_engine(upload_path, fields)
        except Exception as exc:
            import traceback; traceback.print_exc()
            self.send_html("Error", f"<section><h2>Error al analizar</h2><p>{esc(exc)}</p></section>", 500)
            return
        self.redirect(f"/job/{quote(job['id'])}")

    def send_file(self, raw_path: str) -> None:
        path = Path(raw_path).resolve()
        try:
            path.relative_to(ROOT.resolve())
        except ValueError:
            self.send_html("Acceso denegado", "<section><h2>Acceso denegado</h2></section>", 403)
            return
        if not path.exists() or not path.is_file():
            self.send_html("No encontrado", "<section><h2>Archivo no encontrado</h2></section>", 404)
            return
        content_types = {
            ".pdf": "application/pdf",
            ".json": "application/json",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        content_type = content_types.get(path.suffix.lower(), "application/octet-stream")
        payload = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    host = "127.0.0.1"
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8088
    UPLOADS.mkdir(parents=True, exist_ok=True)
    init_history(STORAGE / "history.sqlite")
    httpd = ThreadingHTTPServer((host, port), App)
    print(f"Planos Cotas web listo en http://{host}:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
