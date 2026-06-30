"""In-process or auto-spawned local Roteiro Viral API (no manual ROTEIRO_VIRAL_API)."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx

from pathlib import Path

from .bootstrap import ensure_runtime_on_path, qclaw_root, resolve_runtime_root

log = logging.getLogger(__name__)

_client: Optional[httpx.Client] = None
_subprocess: Optional[subprocess.Popen[Any]] = None
_ui_client: Optional[httpx.Client] = None
_ui_subprocess: Optional[subprocess.Popen[Any]] = None
_lock = __import__("threading").Lock()
_init_error: Optional[str] = None
_mode: str = ""


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _runtime_python(root: Any) -> str:
    root_path = Path(root)
    for candidate in (
        root_path / ".venv" / "bin" / "python3",
        root_path / ".venv" / "bin" / "python",
        root_path / ".venv" / "Scripts" / "python.exe",
        qclaw_root() / ".venv" / "bin" / "python3",
        qclaw_root() / ".venv" / "bin" / "python",
        qclaw_root() / ".venv" / "Scripts" / "python.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _find_frontend_dist(root: Path) -> str | None:
    from .bootstrap import resolve_frontend_dist

    return resolve_frontend_dist(runtime_root=root)


def _apply_subprocess_env(env: dict[str, str], root: Path) -> None:
    """Point embedded uvicorn at the built SPA and RV runtime root."""
    rv_path = (os.environ.get("ROTEIRO_VIRAL_PATH") or str(root)).strip()
    if rv_path:
        env.setdefault("ROTEIRO_VIRAL_PATH", rv_path)
    dist = _find_frontend_dist(root)
    if dist:
        env["ROTEIRO_VIRAL_FRONTEND_DIST"] = dist
    root_str = str(root.resolve())
    prefix = env.get("PYTHONPATH", "")
    parts = [p for p in prefix.split(os.pathsep) if p]
    if root_str not in parts:
        env["PYTHONPATH"] = root_str + (os.pathsep + prefix if prefix else "")


def _wait_health(base_url: str, *, timeout: float = 180.0) -> None:
    deadline = time.time() + timeout
    last_err = ""
    with httpx.Client(base_url=base_url, timeout=5.0) as probe:
        while time.time() < deadline:
            try:
                r = probe.get("/health")
                if r.status_code < 500:
                    return
                last_err = f"HTTP {r.status_code}"
            except Exception as exc:
                last_err = str(exc)
            time.sleep(1.0)
    raise RuntimeError(f"RV local server não respondeu a /health: {last_err}")


def _build_inprocess_client() -> httpx.Client:
    ensure_runtime_on_path()
    from api_v2.main import app  # type: ignore[import-untyped]

    transport = httpx.ASGITransport(app=app)
    return httpx.Client(
        transport=transport,
        base_url="http://roteiro-viral.local",
        timeout=httpx.Timeout(120.0, connect=30.0),
        follow_redirects=True,
    )


def _spawn_subprocess_client() -> httpx.Client:
    root = resolve_runtime_root()
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    python = _runtime_python(root)
    env = os.environ.copy()
    ensure_runtime_on_path()
    _apply_subprocess_env(env, root)
    cmd = [
        python,
        "-m",
        "uvicorn",
        "api_v2.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    log.info("Starting local RV subprocess: %s (cwd=%s)", " ".join(cmd), root)
    proc = subprocess.Popen(
        cmd,
        cwd=str(root),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if proc.poll() is not None:
        err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", errors="replace")
        raise RuntimeError(f"RV subprocess exit {proc.returncode}: {err[:800]}")
    _wait_health(base_url)
    return httpx.Client(
        base_url=base_url,
        timeout=httpx.Timeout(120.0, connect=30.0),
        follow_redirects=True,
    ), proc


def _build_subprocess_client() -> httpx.Client:
    global _subprocess
    client, proc = _spawn_subprocess_client()
    _subprocess = proc
    return client


def get_browser_base_url() -> str:
    """HTTP base reachable from browser iframes (never roteiro-viral.local)."""
    global _ui_client, _ui_subprocess
    client = get_client()
    base = str(client.base_url).rstrip("/")
    if base and not base.startswith("http://roteiro-viral."):
        return base
    with _lock:
        if _ui_client is not None:
            return str(_ui_client.base_url).rstrip("/")
        _ui_client, _ui_subprocess = _spawn_subprocess_client()
        return str(_ui_client.base_url).rstrip("/")


def get_client() -> httpx.Client:
    global _client, _init_error, _mode
    with _lock:
        if _client is not None:
            return _client
        if _init_error:
            raise RuntimeError(_init_error)

        root = resolve_runtime_root()
        prefer_subprocess = (
            os.environ.get("QCLAW_RV_SUBPROCESS_ONLY", "").strip().lower() in ("1", "true", "yes")
            or _runtime_python(root) != sys.executable
        )

        errors: list[str] = []
        builders = (
            [_build_subprocess_client, _build_inprocess_client]
            if prefer_subprocess
            else [_build_inprocess_client, _build_subprocess_client]
        )
        for build in builders:
            try:
                _client = build()
                _mode = "subprocess" if build is _build_subprocess_client else "inprocess"
                log.info("Embedded Roteiro Viral API ready (%s)", _mode)
                return _client
            except Exception as exc:
                label = "subprocess" if build is _build_subprocess_client else "in-process"
                errors.append(f"{label}: {exc}")
                log.warning("RV %s startup failed: %s", label, exc)

        _init_error = "; ".join(errors)
        raise RuntimeError(f"Embedded RV API: {_init_error}")


def client_mode() -> str:
    return _mode or "none"


def active_subprocess_pids() -> set[int]:
    """PIDs of embedded RV uvicorn subprocesses owned by this gois instance."""
    pids: set[int] = set()
    with _lock:
        for proc in (_subprocess, _ui_subprocess):
            if proc is not None and proc.poll() is None:
                pids.add(proc.pid)
    return pids


def reset_client() -> None:
    """For tests — tear down singleton."""
    global _client, _init_error, _subprocess, _mode, _ui_client, _ui_subprocess
    with _lock:
        if _client is not None:
            _client.close()
        _client = None
        _init_error = None
        _mode = ""
        if _subprocess is not None:
            _subprocess.terminate()
            try:
                _subprocess.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _subprocess.kill()
            _subprocess = None
        if _ui_client is not None:
            _ui_client.close()
        _ui_client = None
        if _ui_subprocess is not None:
            _ui_subprocess.terminate()
            try:
                _ui_subprocess.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _ui_subprocess.kill()
            _ui_subprocess = None


def _parse_path(path: str) -> tuple[str, dict[str, str]]:
    parsed = urlparse(path if "://" in path else f"http://local{path}")
    route = parsed.path or "/"
    query = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
    return route, query


def _response_to_dict(response: httpx.Response) -> dict[str, Any]:
    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        raise RuntimeError(f"HTTP {response.status_code}: {detail}")

    content_type = (response.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        data = response.json()
        return data if isinstance(data, dict) else {"data": data}

    if response.content:
        return {"_binary": response.content, "file_path": ""}
    return {}


def request_embedded(
    method: str,
    path: str,
    *,
    payload: Optional[dict[str, Any]] = None,
    file_bytes: Optional[bytes] = None,
    file_name: str = "upload.bin",
    file_mime: str = "application/octet-stream",
    timeout: float = 120.0,
) -> dict[str, Any]:
    client = get_client()
    route, query = _parse_path(path)
    m = method.upper()

    kwargs: dict[str, Any] = {"timeout": timeout}
    if query:
        kwargs["params"] = query

    if file_bytes is not None:
        data = {k: str(v) for k, v in (payload or {}).items() if v is not None}
        kwargs["data"] = data
        kwargs["files"] = {"file": (file_name, file_bytes, file_mime)}
        response = client.request(m, route, **kwargs)
    elif m in ("POST", "PUT", "PATCH"):
        response = client.request(m, route, json=payload or {}, **kwargs)
    else:
        response = client.request(m, route, **kwargs)

    return _response_to_dict(response)


def embedded_available() -> bool:
    try:
        resolve_runtime_root()
        return True
    except Exception:
        return False
