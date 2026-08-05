from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 8088


def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    if is_port_open(port):
        print(f"Planos Cotas ya esta corriendo en http://127.0.0.1:{port}")
        return

    storage = ROOT / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    out_log = storage / "web_app.out.log"
    err_log = storage / "web_app.err.log"

    env = dict(os.environ)
    if "PATH" in env and "Path" in env:
        env["Path"] = env.get("Path") or env["PATH"]
        env.pop("PATH", None)

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

    with out_log.open("ab") as stdout, err_log.open("ab") as stderr:
        subprocess.Popen(
            [sys.executable, str(ROOT / "tools" / "web_app.py"), str(port)],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            env=env,
            close_fds=True,
            creationflags=creationflags,
        )

    for _ in range(20):
        if is_port_open(port):
            print(f"Planos Cotas listo en http://127.0.0.1:{port}")
            return
        import time

        time.sleep(0.2)

    raise SystemExit(f"No se pudo confirmar el arranque. Revise {err_log}")


if __name__ == "__main__":
    main()
