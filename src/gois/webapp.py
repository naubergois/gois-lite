from __future__ import annotations

import argparse
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from .env_util import load_dotenv
from .gois_lite import is_gois_lite
from .platform_paths import is_macos, is_windows, user_log_dir, venv_python
from .webapp_cocoa_popups import enable_cocoa_popups
from .webapp_cocoa_media import enable_cocoa_media_capture, prime_macos_microphone_access

_LOG_DIR = user_log_dir()
_LOG_FILE = _LOG_DIR / "webapp.log"


def _setup_logging() -> logging.Logger:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("gois.webapp")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        fh = RotatingFileHandler(
            _LOG_FILE,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger


def _osascript_alert(title: str, message: str) -> None:
    """Best-effort native alert. No-op outside macOS — logs instead."""
    if not is_macos():
        logging.getLogger("gois.webapp").info("%s: %s", title, message)
        return
    osa = shutil.which("osascript") or "/usr/bin/osascript"
    if not Path(osa).exists():
        return
    safe = message.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(
        [osa, "-e", f'display alert "{title}" message "{safe}"'],
        check=False,
    )


def _status_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urlopen(url, timeout=timeout) as resp:  # nosec B310 (localhost only)
            return int(getattr(resp, "status", 0)) == 200
    except (URLError, OSError, TimeoutError, ValueError):
        return False


def _monitor_ready_url(port: int) -> str:
    """Lightweight readiness probe — avoid downloading the full /status payload."""
    return f"http://127.0.0.1:{port}/ready"


def _port_open(host: str, port: int, *, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _gois_process_lines(project_dir: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["pgrep", "-fl", "gois"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    root = str(project_dir)
    return [line for line in proc.stdout.splitlines() if root in line]


def _monitor_process_running(project_dir: Path) -> bool:
    """True when ``python -m gois`` for this project is already alive."""
    for line in _gois_process_lines(project_dir):
        if " -m gois" in line:
            return True
    return False


def _monitor_startup_in_progress(project_dir: Path) -> bool:
    """True when a restart/bootstrap is already running (avoid restart storms)."""
    script_markers = (
        "scripts/restart.sh",
        "scripts/start.sh",
        "migrate_all_to_mongo",
        "migrate_runtime_to_redis",
        "install-launchd.sh",
    )
    for line in _gois_process_lines(project_dir):
        if any(marker in line for marker in script_markers):
            return True
        if " -m gois" in line:
            # Monitor is bootstrapping (Mongo migrate runs before HTTP listens).
            return True
    return False


def _open_browser(url: str, logger: logging.Logger, *, alert: bool) -> None:
    logger.info("opening system browser: %s", url)
    opened = False
    try:
        # webbrowser handles macOS/Linux/Windows uniformly and never escalates.
        opened = webbrowser.open(url, new=2, autoraise=True)
    except Exception as exc:
        logger.warning("webbrowser.open failed: %s", exc)
    if not opened:
        # Last-resort OS-native openers (still user-mode, never sudo)
        try:
            if is_windows():
                os.startfile(url)  # type: ignore[attr-defined]
                opened = True
            elif is_macos():
                opener = shutil.which("open") or "/usr/bin/open"
                subprocess.run([opener, url], check=False)
                opened = True
            else:
                opener = shutil.which("xdg-open")
                if opener:
                    subprocess.run([opener, url], check=False)
                    opened = True
        except Exception as exc:
            logger.warning("native opener failed: %s", exc)
    if alert:
        _osascript_alert(
            "Gois",
            f"Opened dashboard in your default browser:\n{url}",
        )


def _ensure_monitor(
    project_dir: Path, wait_seconds: float, logger: logging.Logger
) -> str:
    env_file = project_dir / ".env"
    n = load_dotenv(env_file)
    if n:
        logger.info("loaded %d var(s) from %s", n, env_file)

    port = 9101
    # Fast path: YAML is enough for the HTTP port and avoids blocking on Mongo
    # during desktop startup (migrate_all_to_mongo can take tens of seconds).
    try:
        import yaml

        cfg_path = project_dir / "config.yaml"
        if cfg_path.is_file():
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
            port = int((cfg.get("http") or {}).get("port", 9101))
    except Exception as exc:
        logger.warning(
            "could not read config for port, using %d: %s", 9101, exc
        )
        port = 9101
    url = f"http://127.0.0.1:{port}/"
    ready_url = _monitor_ready_url(port)
    probe_timeout = min(5.0, max(2.0, wait_seconds / 10.0))

    if _status_ok(ready_url, timeout=probe_timeout):
        logger.info("monitor already running at %s", url)
        return url

    # Avoid unnecessary restarts on transient startup races: retry briefly
    # before triggering restart scripts.
    grace_deadline = time.time() + min(15.0, max(3.0, wait_seconds / 3.0))
    while time.time() < grace_deadline:
        if _status_ok(ready_url, timeout=probe_timeout):
            logger.info("monitor became ready during grace probe at %s", url)
            return url
        if _monitor_process_running(project_dir) or _port_open("127.0.0.1", port):
            break
        time.sleep(0.25)

    startup_busy = _monitor_startup_in_progress(project_dir)
    if startup_busy:
        logger.info(
            "monitor bootstrap already in progress; waiting up to %.1fs",
            wait_seconds,
        )
    else:
        logger.info("monitor not ready; attempting start/restart")
        bash = shutil.which("bash")
        py = venv_python(project_dir)
        py_exec = str(py) if py.is_file() else sys.executable

        restart_sh = project_dir / "scripts" / "restart.sh"
        install_sh = project_dir / "scripts" / "install-launchd.sh"
        restart_ps1 = project_dir / "scripts" / "restart.ps1"
        start_ps1 = project_dir / "scripts" / "start.ps1"

        cmd: list[str] | None = None
        env_file = project_dir / ".env"
        is_lite = is_gois_lite() or (project_dir / ".gois-lite").is_file()
        try:
            if is_windows():
                ps = shutil.which("pwsh") or shutil.which("powershell")
                if ps and restart_ps1.is_file():
                    cmd = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(restart_ps1)]
                elif ps and start_ps1.is_file():
                    cmd = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(start_ps1)]
                else:
                    # Fallback: spawn monitor directly
                    cmd = [py_exec, "-m", "gois", "--config", str(project_dir / "config.yaml")]
            elif is_lite:
                # gois-lite shares no LaunchAgent with the main monitor — spawn directly.
                cmd = [py_exec, "-m", "gois", "--config", str(project_dir / "config.yaml")]
                if env_file.is_file():
                    cmd.extend(["--env-file", str(env_file)])
            elif bash and restart_sh.is_file():
                # kickstart existing LaunchAgent only — avoid reinstalling plist
                # on every app open (that SIGTERM-kills gois mid-bootstrap).
                cmd = [bash, str(restart_sh), "--skip-vendor"]
            elif bash and install_sh.is_file():
                cmd = [bash, str(install_sh)]
            else:
                cmd = [py_exec, "-m", "gois", "--config", str(project_dir / "config.yaml")]
            if cmd:
                logger.info("running %s", " ".join(cmd))
                subprocess.Popen(  # noqa: S603 - args are absolute/known
                    cmd,
                    cwd=project_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
        except Exception as exc:
            logger.warning("restart attempt failed: %s", exc)

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if _status_ok(ready_url, timeout=probe_timeout):
            logger.info("monitor ready at %s", url)
            return url
        time.sleep(0.4)

    logger.warning(
        "monitor still not responding after %.1fs; loading %s anyway",
        wait_seconds,
        url,
    )
    return url


def _run_pywebview(url: str, logger: logging.Logger) -> int:
    try:
        import webview
    except Exception as exc:
        logger.warning("pywebview not available: %s", exc)
        return 1

    try:
        enable_cocoa_popups()
        enable_cocoa_media_capture()
        prime_macos_microphone_access()
        title = "Gois Lite" if is_gois_lite() else "Gois"
        window = webview.create_window(
            title,
            url=url,
            width=1380,
            height=920,
            min_size=(980, 680),
        )
        webview.start(gui="cocoa", debug=False, private_mode=False)
        if window is None:
            logger.error("pywebview returned no window")
            return 1
        return 0
    except Exception:
        logger.exception("pywebview failed")
        return 1


def main() -> None:
    logger = _setup_logging()
    parser = argparse.ArgumentParser(
        prog="gois-webapp",
        description="Native desktop window for gois dashboard.",
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
        help="gois project root",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=120.0,
        help="wait for monitor /ready before loading (launchd+migrate can be slow)",
    )
    parser.add_argument(
        "--browser-only",
        action="store_true",
        help="open the dashboard in the default browser instead of pywebview",
    )
    args = parser.parse_args()

    project_dir = args.project_dir.expanduser().resolve()
    venv_py = venv_python(project_dir)
    if not venv_py.is_file():
        bin_dir = "Scripts" if is_windows() else "bin"
        msg = (
            f"Python venv not found at {venv_py}. "
            f"Create it with: python -m venv .venv && .venv/{bin_dir}/pip install -e ."
        )
        logger.error(msg)
        _osascript_alert("Gois", msg)
        raise SystemExit(2)

    logger.info("project_dir=%s", project_dir)
    url = _ensure_monitor(project_dir, max(1.0, args.wait_seconds), logger)

    if args.browser_only:
        _open_browser(url, logger, alert=False)
        return

    rc = _run_pywebview(url, logger)
    if rc != 0:
        logger.info("falling back to system browser (pywebview rc=%d)", rc)
        _open_browser(url, logger, alert=True)


if __name__ == "__main__":
    main()
