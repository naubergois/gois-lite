"""Concrete recovery actions exposed to the LLM agent.

Each method is intentionally narrow: it does one thing, returns a string the
agent can reason about, and refuses anything outside the allowlist in config.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

import httpx

from .config import HermesCronRecoveryConfig, OpenclawDoctorConfig, QclawConfig
from .hermes_cron import (
    hermes_cron_argv,
    hermes_gateway_argv,
    hermes_home_from_jobs_path,
    probe_hermes_cron_scheduler,
    resolve_hermes_bin,
)
from .local_paths import _repo_root, openclaw_bundle_base, openclaw_state_dir
from .openclaw_config_repair import (
    default_openclaw_config_paths,
    repair_openclaw_config_files,
)
from .whatsapp_allowlist import whatsapp_cron_guard_env


# Default install location for the QClaw-bundled openclaw CLI on macOS.
_DEFAULT_OPENCLAW_BASE = openclaw_bundle_base()

_CRON_JOB_NAME_RE = re.compile(r"Job '([^']+)' failed")
_GATEWAY_PORT_ENV_RE = re.compile(r"OPENCLAW_GATEWAY_PORT=(\d+)")
_GATEWAY_PORT_ARG_RE = re.compile(r"--port[=\s]+(\d+)")
# The launchd-managed gateway runs as `node …/openclaw/dist/index.js gateway
# --port N` — the literal string "openclaw-gateway" never appears on its
# command line, and pgrep does not expose env vars, so match both forms.
_GATEWAY_PROC_PATTERNS = ("openclaw-gateway", "index.js gateway")


def _gateway_port_listening(port: int, host: str = "127.0.0.1") -> bool:
    """True when something accepts TCP connections on host:port."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _discover_openclaw_gateway_port() -> Optional[int]:
    """Find the live gateway port from running openclaw gateway processes.

    Reads the port from the process command line — both the ``--port N`` flag
    the gateway is launched with and an ``OPENCLAW_GATEWAY_PORT=`` env echo, if
    present. A listening port wins; otherwise the most recent candidate.
    """
    import shutil

    if not shutil.which("pgrep"):
        return None
    ports: list[int] = []
    for pattern in _GATEWAY_PROC_PATTERNS:
        try:
            proc = subprocess.run(
                ["pgrep", "-fl", pattern],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        for line in proc.stdout.splitlines():
            for rx in (_GATEWAY_PORT_ARG_RE, _GATEWAY_PORT_ENV_RE):
                m = rx.search(line)
                if m:
                    try:
                        ports.append(int(m.group(1)))
                    except ValueError:
                        pass
    # De-dupe while preserving discovery order.
    seen: set[int] = set()
    unique = [p for p in ports if not (p in seen or seen.add(p))]
    if not unique:
        return None
    for port in unique:
        if _gateway_port_listening(port):
            return port
    return unique[-1]


def parse_hermes_cron_job_name(log_line: str) -> Optional[str]:
    """Extract the cron job name from a Hermes scheduler failure log line."""
    m = _CRON_JOB_NAME_RE.search(log_line)
    return m.group(1) if m else None


def resolve_hermes_cron_job_id(job_name: str, jobs_path: Path) -> Optional[str]:
    """Look up a cron job ID by its display name in jobs.json."""
    from .hermes_cron import find_job_id_by_name

    return find_job_id_by_name(job_name, jobs_path)


def _gateway_port_file_stale(port_file: Path) -> bool:
    """True when qclaw.json still points at a dead CLI session."""
    try:
        data = json.loads(port_file.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    cli = data.get("cli")
    if not isinstance(cli, dict):
        return False
    raw_pid = cli.get("pid")
    if raw_pid is None:
        return False
    try:
        os.kill(int(raw_pid), 0)
    except (OSError, ValueError, TypeError):
        return True
    return False


def _maybe_sync_gateway_port_file(port_file: Path, port: int) -> None:
    """Update qclaw.json when the live gateway port drifts from the file."""
    try:
        data = json.loads(port_file.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if data.get("port") == port and not _gateway_port_file_stale(port_file):
        return
    data["port"] = port
    if _gateway_port_file_stale(port_file):
        cli = data.get("cli")
        if isinstance(cli, dict):
            cli.pop("pid", None)
    try:
        port_file.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


def _resolve_live_gateway_port(
    cfg: QclawConfig, *, sync_file: bool = True
) -> Optional[int]:
    """Return a gateway port that is listening, discovering from processes if needed."""
    port_file = (
        Path(cfg.gateway_port_file).expanduser()
        if cfg.gateway_port_file
        else None
    )
    file_port: Optional[int] = None
    stale = False
    should_discover = False
    if port_file and port_file.is_file():
        stale = _gateway_port_file_stale(port_file)
        if not stale:
            try:
                data = json.loads(port_file.read_text())
                raw = data.get("port")
                if raw is not None:
                    file_port = int(raw)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        if file_port is not None and _gateway_port_listening(file_port):
            return file_port
        should_discover = stale or file_port is not None

    discovered = _discover_openclaw_gateway_port() if should_discover else None
    if discovered:
        if sync_file and port_file and port_file.is_file():
            _maybe_sync_gateway_port_file(port_file, discovered)
        return discovered

    if file_port is not None and not stale:
        return file_port
    return None


def _resolve_health_url(cfg: QclawConfig) -> Optional[str]:
    """Return the HTTP probe URL, preferring the live port from qclaw.json."""
    base = cfg.health_url
    live_port = _resolve_live_gateway_port(cfg)
    if live_port is None:
        return base
    if base:
        parsed = urlparse(base)
        host = parsed.hostname or "127.0.0.1"
        scheme = parsed.scheme or "http"
        path = parsed.path or "/"
        return urlunparse((scheme, f"{host}:{live_port}", path, "", "", ""))
    return f"http://127.0.0.1:{live_port}/"


def _resolve_openclaw_paths(
    cfg: OpenclawDoctorConfig,
) -> tuple[Optional[Path], Optional[Path], Optional[Path]]:
    """Resolve (bin, node, mjs). Any missing piece returns None for that slot
    and the caller turns it into a clear error message."""
    base = _DEFAULT_OPENCLAW_BASE
    bin_path = Path(cfg.bin_path).expanduser() if cfg.bin_path else base / "config/bin/openclaw"
    node_path = Path(cfg.node_path).expanduser() if cfg.node_path else base / "config/bin/node"
    mjs_path = (
        Path(cfg.mjs_path).expanduser()
        if cfg.mjs_path
        else base / "node_modules/openclaw/openclaw.mjs"
    )
    return (
        bin_path if bin_path.exists() else None,
        node_path if node_path.exists() else None,
        mjs_path if mjs_path.exists() else None,
    )


class Recovery:
    def __init__(self, cfg: QclawConfig):
        self.cfg = cfg
        self._hang_consecutive_failures = 0

    def _apply_hang_failure_threshold(self, result: dict) -> dict:
        """Debounce UI hang probes before they fail the aggregate health check."""
        if result.get("skipped") or result.get("ok"):
            self._hang_consecutive_failures = 0
            return result
        if not result.get("hang"):
            return result

        threshold = max(1, int(self.cfg.hang_check.failure_threshold))
        self._hang_consecutive_failures += 1
        if self._hang_consecutive_failures < threshold:
            debounced = dict(result)
            debounced["ok"] = True
            debounced["debounced"] = True
            debounced["consecutive_hangs"] = self._hang_consecutive_failures
            debounced["failure_threshold"] = threshold
            return debounced
        result = dict(result)
        result["consecutive_hangs"] = self._hang_consecutive_failures
        result["failure_threshold"] = threshold
        return result

    async def health_check(self) -> dict:
        """Aggregate every configured signal. `ok` is True only when every
        configured check passes. Always returns a dict with `checks`."""
        checks: dict = {}
        if self.cfg.process_pattern:
            checks["process"] = await self._check_process(self.cfg.process_pattern)
        if _resolve_health_url(self.cfg):
            checks["http"] = await self._check_http()
        if self.cfg.hang_check.enabled:
            responsive = await self._check_responsive()
            checks["responsive"] = self._apply_hang_failure_threshold(responsive)
        ok = bool(checks) and all(c.get("ok") for c in checks.values())
        if (
            not ok
            and getattr(self.cfg, "name", None) == "hermes"
            and checks.get("http", {}).get("ok")
            and not (checks.get("process") or {}).get("ok")
        ):
            dash = await self._check_process(" dashboard ")
            if dash.get("ok"):
                proc = dict(checks.get("process") or {})
                proc.update(
                    {
                        "ok": True,
                        "pids": dash.get("pids") or proc.get("pids") or [],
                        "matched_mode": "dashboard",
                    }
                )
                checks["process"] = proc
            else:
                checks["process"] = {
                    **(checks.get("process") or {}),
                    "ok": True,
                    "skipped": True,
                    "reason": "http_ok_dashboard_mode",
                }
            ok = all(c.get("ok") for c in checks.values())
        return {"ok": ok, "checks": checks}

    @staticmethod
    def _scan_pids_psutil(pattern: str) -> list[str]:
        import psutil  # type: ignore

        pids: list[str] = []
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = p.info.get("name") or ""
                cmd = " ".join(p.info.get("cmdline") or [])
                if pattern in name or pattern in cmd:
                    pids.append(str(p.info["pid"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return pids

    async def _pgrep_pids(self, pattern: str, *, attempts: int = 3) -> list[str]:
        """Run pgrep with short retries when fork() returns EAGAIN under load."""
        last_exc: BaseException | None = None
        for attempt in range(max(1, attempts)):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "pgrep", "-f", pattern,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, _ = await proc.communicate()
                return [p for p in out.decode().strip().splitlines() if p]
            except (BlockingIOError, OSError) as exc:
                errno = getattr(exc, "errno", None)
                if errno not in (11, 35) and not isinstance(exc, BlockingIOError):
                    raise
                last_exc = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.25 * (attempt + 1))
        if last_exc is not None:
            raise last_exc
        return []

    async def _check_process(self, pattern: str) -> dict:
        # Prefer psutil (no fork storm) when installed; pgrep is the fallback on
        # systems without psutil. Under heavy load fork can fail with EAGAIN.
        try:
            import psutil  # type: ignore  # noqa: F401
        except ImportError:
            psutil = None  # type: ignore[assignment,misc]
        else:
            try:
                pids = await asyncio.to_thread(self._scan_pids_psutil, pattern)
                return {
                    "ok": len(pids) > 0,
                    "pattern": pattern,
                    "pids": pids,
                    "method": "psutil",
                }
            except Exception:
                pass

        import shutil as _shutil
        if _shutil.which("pgrep"):
            try:
                pids = await self._pgrep_pids(pattern)
                return {
                    "ok": len(pids) > 0,
                    "pattern": pattern,
                    "pids": pids,
                    "method": "pgrep",
                }
            except (BlockingIOError, OSError) as exc:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": f"pgrep fork failed under load ({exc})",
                    "pattern": pattern,
                    "pids": [],
                }
        if psutil is None:
            return {
                "ok": True,
                "skipped": True,
                "reason": "neither pgrep nor psutil available",
                "pattern": pattern,
            }
        return {"ok": False, "pattern": pattern, "pids": [], "error": "psutil scan failed"}

    async def _check_responsive(self) -> dict:
        """Detect a frozen GUI: ask System Events a trivial question about the
        process and enforce a hard wall-clock timeout. A live app answers in
        milliseconds; a hung app blocks until we kill the osascript subprocess.

        Permission errors (Accessibility not granted) are reported as `ok=True`
        with `skipped=True` so they don't trip recovery in a loop."""
        app_name = self.cfg.hang_check.app_name or self.cfg.name
        timeout = self.cfg.hang_check.timeout_seconds
        # osascript is macOS-only — on Linux/Windows we cannot probe a GUI
        # hang the same way, so we skip without tripping recovery loops.
        import platform as _platform
        import shutil as _shutil
        if _platform.system() != "Darwin" or not _shutil.which("osascript"):
            return {
                "ok": True,
                "skipped": True,
                "reason": "hang_check requires macOS osascript",
                "app": app_name,
            }
        script = (
            f'tell application "System Events" to '
            f'tell process "{app_name}" to count windows'
        )
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
            return {
                "ok": False,
                "hang": True,
                "app": app_name,
                "timeout_seconds": timeout,
                "reason": "AppleScript probe timed out (UI unresponsive)",
            }
        stderr_text = err.decode(errors="replace").strip()
        # macOS error -1719 = process not found, -1728 = invalid index,
        # -25211/-1743 = Accessibility / automation permission denied.
        if proc.returncode != 0:
            permission_error = any(
                code in stderr_text for code in ("-1743", "-25211", "not authorized")
            )
            not_running = any(
                token in stderr_text
                for token in ("-1719", "-1728", "isn't running", "Não é possível obter process")
            )
            if permission_error:
                return {
                    "ok": True,
                    "skipped": True,
                    "app": app_name,
                    "reason": (
                        "AppleScript not authorized; grant Accessibility / Automation "
                        f"permission to probe {app_name!r}. Detail: {stderr_text[:200]}"
                    ),
                }
            if not_running:
                return {
                    "ok": False,
                    "app": app_name,
                    "reason": f"process {app_name!r} not running",
                }
            return {
                "ok": False,
                "app": app_name,
                "rc": proc.returncode,
                "reason": stderr_text[:300] or "AppleScript failed",
            }
        return {
            "ok": True,
            "app": app_name,
            "windows": out.decode(errors="replace").strip(),
        }

    async def _check_http(self) -> dict:
        url = _resolve_health_url(self.cfg)
        if not url:
            return {"ok": False, "url": None, "error": "no health_url configured"}
        # Gateway can stall briefly under load (cron, telegram, skill sync).
        # Retry once on transport timeouts before reporting down.
        timeout = httpx.Timeout(
            self.cfg.timeout_seconds,
            connect=min(3.0, self.cfg.timeout_seconds),
        )
        last_error: Optional[str] = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    r = await client.get(url)
                    return {
                        "ok": r.status_code == self.cfg.expected_status,
                        "url": url,
                        "status": r.status_code,
                        "body": r.text[:300],
                        **({"retries": attempt} if attempt else {}),
                    }
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError) as e:
                last_error = f"{type(e).__name__}: {e}"
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
            except Exception as e:
                return {
                    "ok": False,
                    "url": url,
                    "error": f"{type(e).__name__}: {e}",
                }
        return {"ok": False, "url": url, "error": last_error or "unknown", "retries": 1}

    async def process_status(self) -> str:
        """Broad process listing for the agent to inspect related processes.

        Uses ``pgrep`` when available (macOS/Linux); falls back to psutil so
        Windows works without admin and without bundled CLI tools.
        """
        import shutil as _shutil
        if _shutil.which("pgrep"):
            proc = await asyncio.create_subprocess_exec(
                "pgrep", "-fl", self.cfg.name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate()
            text = out.decode(errors="replace").strip()
            return text or f"(no process matching {self.cfg.name!r})"
        try:
            import psutil  # type: ignore
        except ImportError:
            return f"(pgrep/psutil unavailable; cannot list processes for {self.cfg.name!r})"
        rows: list[str] = []
        needle = self.cfg.name
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = p.info.get("name") or ""
                cmd = " ".join(p.info.get("cmdline") or [])
                if needle in name or needle in cmd:
                    rows.append(f"{p.info['pid']} {cmd or name}")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return "\n".join(rows) or f"(no process matching {self.cfg.name!r})"

    async def read_log_tail(self, path: str, lines: int = 200) -> str:
        if path not in self.cfg.log_paths:
            allowed = ", ".join(self.cfg.log_paths) or "(none)"
            return f"refused: {path!r} not in allowed log_paths. Allowed: {allowed}"
        p = Path(path)
        if not p.exists():
            return f"log not found: {path}"
        lines = max(1, min(int(lines), 2000))
        # Cross-OS tail: prefer the system ``tail`` (POSIX); fall back to a
        # pure-Python tail on Windows or stripped containers.
        import shutil as _shutil
        if _shutil.which("tail"):
            proc = await asyncio.create_subprocess_exec(
                "tail", "-n", str(lines), str(p),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            if proc.returncode != 0:
                return f"tail rc={proc.returncode}: {err.decode(errors='replace')}"
            return out.decode(errors="replace")
        try:
            # Bounded memory: read tail by walking from EOF backwards.
            block = 8192
            data = bytearray()
            line_count = 0
            with open(p, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                pos = fh.tell()
                while pos > 0 and line_count <= lines:
                    read = min(block, pos)
                    pos -= read
                    fh.seek(pos)
                    chunk = fh.read(read)
                    data[:0] = chunk
                    line_count = data.count(b"\n")
            text = data.decode("utf-8", errors="replace")
            tail_lines = text.splitlines()[-lines:]
            return "\n".join(tail_lines)
        except OSError as e:
            return f"read failed: {type(e).__name__}: {e}"

    async def openclaw_doctor_fix(self, doctor_cfg: OpenclawDoctorConfig) -> dict:
        """Run `openclaw doctor --fix` with a hard wall-clock timeout.

        Returns a structured dict (always) describing what happened. The
        caller is responsible for cooldown — this method just executes.
        """
        channel_repairs = repair_openclaw_config_files(
            default_openclaw_config_paths(state_dir=openclaw_state_dir()),
        )
        channel_fix_count = sum(1 for r in channel_repairs if r.changed)

        bin_path, node_path, mjs_path = _resolve_openclaw_paths(doctor_cfg)
        if not bin_path or not node_path or not mjs_path:
            missing = [
                label
                for label, p in (
                    ("bin", bin_path), ("node", node_path), ("mjs", mjs_path),
                )
                if p is None
            ]
            return {
                "ok": False,
                "reason": (
                    f"openclaw CLI not found (missing: {', '.join(missing)}). "
                    "Set openclaw_doctor.bin_path / node_path / mjs_path in config."
                ),
            }

        env = {
            **os.environ,
            "QCLAW_CLI_NODE_BINARY": str(node_path),
            "QCLAW_CLI_OPENCLAW_MJS": str(mjs_path),
        }
        proc = await asyncio.create_subprocess_exec(
            str(bin_path), "doctor", "--fix",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=doctor_cfg.timeout_seconds
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
            return {
                "ok": False,
                "timeout": True,
                "timeout_seconds": doctor_cfg.timeout_seconds,
                "reason": f"openclaw doctor --fix timed out after {doctor_cfg.timeout_seconds}s",
            }
        stdout_text = out.decode(errors="replace")
        stderr_text = err.decode(errors="replace")

        # doctor --fix can re-merge broken channels from the QClaw template.
        post_repairs = repair_openclaw_config_files(
            default_openclaw_config_paths(state_dir=openclaw_state_dir()),
        )
        post_fix_count = sum(1 for r in post_repairs if r.changed)

        summary = (
            stdout_text.strip().splitlines()[-1]
            if stdout_text.strip()
            else (stderr_text.strip().splitlines()[-1] if stderr_text.strip() else "(no output)")
        )[:300]
        if channel_fix_count or post_fix_count:
            summary = (
                f"channel scrub pre={channel_fix_count} post={post_fix_count}; {summary}"
            )[:300]

        return {
            "ok": proc.returncode == 0,
            "rc": proc.returncode,
            "stdout_tail": stdout_text[-2000:],
            "stderr_tail": stderr_text[-2000:],
            "summary": summary,
            "channel_scrub_pre": channel_fix_count,
            "channel_scrub_post": post_fix_count,
        }

    async def hermes_cron_command(
        self,
        cmd: list[str],
        *,
        timeout_seconds: float,
        job_id: Optional[str] = None,
        job_name: Optional[str] = None,
        jobs_path: Optional[Path] = None,
    ) -> dict:
        """Run a `hermes cron …` subcommand and return a structured result."""
        pretty = " ".join(shlex.quote(c) for c in cmd)
        env = os.environ.copy()
        if jobs_path is not None:
            env["HERMES_HOME"] = str(hermes_home_from_jobs_path(jobs_path))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
            return {
                "ok": False,
                "timeout": True,
                "job_id": job_id,
                "job_name": job_name,
                "timeout_seconds": timeout_seconds,
                "command": pretty,
                "reason": (
                    f"{pretty} timed out after {timeout_seconds}s"
                ),
            }
        stdout_text = out.decode(errors="replace")
        stderr_text = err.decode(errors="replace")
        return {
            "ok": proc.returncode == 0,
            "rc": proc.returncode,
            "job_id": job_id,
            "job_name": job_name,
            "command": pretty,
            "stdout_tail": stdout_text[-2000:],
            "stderr_tail": stderr_text[-2000:],
            "summary": (
                stdout_text.strip().splitlines()[-1]
                if stdout_text.strip()
                else (
                    stderr_text.strip().splitlines()[-1]
                    if stderr_text.strip()
                    else f"rc={proc.returncode}"
                )
            )[:300],
        }

    async def hermes_cron_retry(
        self, cron_cfg: HermesCronRecoveryConfig, log_line: str
    ) -> dict:
        """Re-run a failed Hermes cron job identified from a log line."""
        job_name = parse_hermes_cron_job_name(log_line)
        if not job_name:
            return {
                "ok": False,
                "reason": f"could not parse job name from log line: {log_line[:200]!r}",
            }
        jobs_path = Path(cron_cfg.jobs_path).expanduser()
        job_id = resolve_hermes_cron_job_id(job_name, jobs_path)
        if not job_id:
            return {
                "ok": False,
                "job_name": job_name,
                "reason": f"job {job_name!r} not found in {jobs_path}",
            }

        if cron_cfg.swarm_only:
            from .hermes_cron import find_job_by_id
            from .swarm_cron_policy import swarm_only_cron_guard

            blocked = swarm_only_cron_guard(
                find_job_by_id(job_id, jobs_path),
                jobs_path=jobs_path,
                job_id=job_id,
            )
            if blocked is not None:
                return {
                    "ok": False,
                    "job_id": job_id,
                    "job_name": job_name,
                    "reason": blocked.get("error"),
                    "swarm_only": True,
                }

        cmd = hermes_cron_argv("run", job_id, accept_hooks=cron_cfg.accept_hooks)
        result = await self.hermes_cron_command(
            cmd,
            timeout_seconds=cron_cfg.timeout_seconds,
            job_id=job_id,
            job_name=job_name,
            jobs_path=jobs_path,
        )
        return result

    async def ensure_hermes_cron_gateway(
        self, cron_cfg: HermesCronRecoveryConfig
    ) -> dict:
        """Start the Hermes gateway for the HERMES_HOME that owns ``cron_cfg.jobs_path``."""
        jobs_path = Path(cron_cfg.jobs_path).expanduser()
        probe = await asyncio.to_thread(probe_hermes_cron_scheduler, jobs_path)
        if probe.get("ok"):
            return {"ok": True, "action": "none", **probe}

        home = Path(str(probe.get("hermes_home") or hermes_home_from_jobs_path(jobs_path)))
        actions: list[str] = []

        start_cmd = hermes_gateway_argv("start")
        actions.append(
            await self._run_hermes_home(start_cmd, home, background=True)
        )
        await asyncio.sleep(3.0)
        probe = await asyncio.to_thread(probe_hermes_cron_scheduler, jobs_path)
        if probe.get("ok"):
            return {
                "ok": True,
                "action": "gateway_start",
                "steps": actions,
                **probe,
            }

        run_cmd = hermes_gateway_argv("run", "--replace")
        actions.append(
            await self._run_hermes_home(run_cmd, home, background=True)
        )
        await asyncio.sleep(4.0)
        probe = await asyncio.to_thread(probe_hermes_cron_scheduler, jobs_path)
        return {
            "ok": bool(probe.get("ok")),
            "action": "gateway_run",
            "steps": actions,
            **probe,
            **(
                {"reason": probe.get("summary") or "cron scheduler still down"}
                if not probe.get("ok")
                else {}
            ),
        }

    async def _run_hermes_home(
        self,
        cmd: list[str],
        home: Path,
        *,
        background: bool = False,
    ) -> str:
        """Run a Hermes CLI command with ``HERMES_HOME`` pinned to ``home``."""
        if cmd and cmd[0] == "hermes":
            cmd = [resolve_hermes_bin(), *cmd[1:]]
        env = whatsapp_cron_guard_env(
            project_dir=_repo_root(),
            env={**os.environ, "HERMES_HOME": str(home.expanduser().resolve())},
        )
        pretty = " ".join(shlex.quote(c) for c in cmd)
        try:
            if background:
                subprocess.Popen(
                    cmd,
                    cwd=self.cfg.working_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env=env,
                )
                return f"spawned (detached, HERMES_HOME={env['HERMES_HOME']}): {pretty}"
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.cfg.working_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            out, err = await proc.communicate()
            return (
                f"$ HERMES_HOME={env['HERMES_HOME']} {pretty}\n"
                f"rc={proc.returncode}\n"
                f"stdout: {out.decode(errors='replace')[:1500]}\n"
                f"stderr: {err.decode(errors='replace')[:1500]}"
            )
        except FileNotFoundError as e:
            return f"command not found: {pretty} ({e})"
        except Exception as e:
            return f"error running {pretty}: {type(e).__name__}: {e}"

    async def hermes_dashboard_up(self) -> bool:
        """True only when the Hermes web UI accepts HTTP on dashboard_url."""
        url = self.cfg.dashboard_url or "http://127.0.0.1:9119"
        timeout = httpx.Timeout(3.0, connect=2.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(url)
                return r.status_code < 500
        except Exception:
            return False

    async def stop_hermes_dashboard(self) -> str:
        """Stop stale `hermes dashboard` processes before a fresh spawn."""
        return await self._run([resolve_hermes_bin(), "dashboard", "--stop"])

    async def start_hermes_dashboard(self, start_command: list[str]) -> str:
        """Spawn `hermes dashboard` detached; append spawn output to Hermes logs."""
        from .local_paths import hermes_home

        home = hermes_home()
        log_dir = home / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "dashboard-spawn.log"
        pretty = " ".join(shlex.quote(c) for c in start_command)
        env = os.environ.copy()
        env["HERMES_HOME"] = str(home)
        try:
            with open(log_path, "a", encoding="utf-8") as log_f:
                log_f.write(f"\n--- spawn {pretty}\n")
                log_f.flush()
                subprocess.Popen(
                    start_command,
                    cwd=self.cfg.working_dir,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=env,
                )
            return f"spawned (detached, log={log_path}): {pretty}"
        except FileNotFoundError as e:
            return f"command not found: {pretty} ({e})"
        except Exception as e:
            return f"error running {pretty}: {type(e).__name__}: {e}"

    async def wait_hermes_dashboard_up(
        self, timeout_seconds: float = 30.0, poll_seconds: float = 1.0,
    ) -> bool:
        import time as _time
        deadline = _time.monotonic() + timeout_seconds
        while _time.monotonic() < deadline:
            if await self.hermes_dashboard_up():
                return True
            await asyncio.sleep(poll_seconds)
        return False

    async def restart(self) -> str:
        if not self.cfg.start_command:
            return (
                "refused: no start_command configured. Recovery agent cannot restart "
                f"{self.cfg.name}; report the diagnosis and let the operator act."
            )
        outputs: list[str] = []
        if self.cfg.stop_command:
            outputs.append(await self._run(self.cfg.stop_command))
            await asyncio.sleep(1.0)
        outputs.append(await self._run(self.cfg.start_command, background=True))
        return "\n".join(outputs)

    async def _run(self, cmd: list[str], background: bool = False) -> str:
        pretty = " ".join(shlex.quote(c) for c in cmd)
        try:
            if background:
                subprocess.Popen(
                    cmd,
                    cwd=self.cfg.working_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return f"spawned (detached): {pretty}"
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.cfg.working_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            return (
                f"$ {pretty}\n"
                f"rc={proc.returncode}\n"
                f"stdout: {out.decode(errors='replace')[:1500]}\n"
                f"stderr: {err.decode(errors='replace')[:1500]}"
            )
        except FileNotFoundError as e:
            return f"command not found: {pretty} ({e})"
        except Exception as e:
            return f"error running {pretty}: {type(e).__name__}: {e}"
