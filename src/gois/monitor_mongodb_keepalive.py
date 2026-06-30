"""MongoDB keepalive loop — auto-restart mongod when it goes down."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


class MonitorMongodbKeepaliveMixin:
    async def _mongodb_keepalive_loop(self) -> None:
        """Periodically ping MongoDB; restart via brew/mongod if unresponsive."""
        mkc = self.cfg.mongodb_keepalive
        await asyncio.sleep(min(15.0, mkc.interval_seconds))
        while True:
            agent = self.tracker.get("mongodb_keepalive")
            if not agent.enabled:
                agent.state = "paused"
            else:
                try:
                    async with self.tracker.track("mongodb_keepalive") as a:
                        up = await self._mongodb_ping()
                        if up:
                            if self.state.mongodb_consecutive_failures > 0:
                                log.info(
                                    "mongodb recovered after %d failures",
                                    self.state.mongodb_consecutive_failures,
                                )
                                await self.notifier.notify(
                                    "info",
                                    "MongoDB recuperado",
                                    f"MongoDB voltou após {self.state.mongodb_consecutive_failures} falha(s).",
                                )
                            self.state.mongodb_consecutive_failures = 0
                            self._mongodb_restart_attempts = 0
                            a.last_result = "up"
                        else:
                            self.state.mongodb_consecutive_failures += 1
                            a.last_result = (
                                f"down ({self.state.mongodb_consecutive_failures}x)"
                            )
                            await self._maybe_restart_mongodb(a)
                        self._persist()
                except Exception as e:
                    log.exception("mongodb keepalive tick crashed: %s", e)
            await asyncio.sleep(mkc.interval_seconds)

    async def _mongodb_ping(self) -> bool:
        from .mongo import ping
        return await asyncio.to_thread(ping, 8000)

    async def _maybe_restart_mongodb(self, agent_ctx: Any) -> None:
        mkc = self.cfg.mongodb_keepalive
        now = time.time()

        if self.state.mongodb_consecutive_failures < mkc.failures_before_restart:
            remaining = (
                mkc.failures_before_restart
                - self.state.mongodb_consecutive_failures
            )
            agent_ctx.last_result = (
                f"down (aguardando {remaining} falha(s) antes de restart)"
            )
            return

        if self._mongodb_restart_attempts >= mkc.max_restart_attempts:
            agent_ctx.last_result = (
                f"down (max {mkc.max_restart_attempts} tentativas esgotadas)"
            )
            return

        last_ts = self._mongodb_last_start_ts
        if now - last_ts < mkc.cooldown_seconds:
            remaining = mkc.cooldown_seconds - (now - last_ts)
            log.info("mongodb down; cooldown %.1fs remaining", remaining)
            agent_ctx.last_result = (
                f"down (cooldown {remaining:.0f}s)"
            )
            return

        self._mongodb_last_start_ts = now
        self._mongodb_restart_attempts += 1

        await self.notifier.notify(
            "warning",
            "MongoDB offline — tentando restart",
            f"Tentativa {self._mongodb_restart_attempts}/{mkc.max_restart_attempts}",
        )

        ok = await self._try_start_mongodb(mkc.start_commands)
        self.state.mongodb_last_restart_ts = now
        self.state.mongodb_last_restart_ok = ok

        if ok:
            self.state.mongodb_last_restart_summary = "restart ok"
            self.state.mongodb_consecutive_failures = 0
            self._mongodb_restart_attempts = 0
            agent_ctx.last_result = "restarted ok"
            log.info("mongodb restarted successfully")
            await self.notifier.notify(
                "info",
                "MongoDB reiniciado com sucesso",
                "O serviço MongoDB foi restaurado automaticamente.",
            )
        else:
            self.state.mongodb_last_restart_summary = (
                f"restart falhou (tentativa {self._mongodb_restart_attempts})"
            )
            agent_ctx.last_result = (
                f"restart falhou ({self._mongodb_restart_attempts}/"
                f"{mkc.max_restart_attempts})"
            )
            log.warning(
                "mongodb restart failed (attempt %d/%d)",
                self._mongodb_restart_attempts,
                mkc.max_restart_attempts,
            )

    async def _try_start_mongodb(
        self, commands: list[list[str]]
    ) -> bool:
        """Try configured shell commands, then mongo_autostart.restart_mongod."""
        from .mongo_autostart import (
            _port_open,
            _uri_port,
            is_local_uri,
            restart_mongod,
        )
        from .mongo_autostart import _mongo_uri

        mkc = self.cfg.mongodb_keepalive
        cmd_timeout = mkc.command_timeout_seconds

        if is_local_uri():
            host = (urlsplit(_mongo_uri()).hostname or "127.0.0.1")
            port = _uri_port()
            if _port_open(host, port):
                # Port is open — mongod is running; wait for ping instead of
                # calling brew restart (which is slow and causes downtime).
                deadline = time.monotonic() + mkc.startup_wait_seconds
                while time.monotonic() < deadline:
                    await asyncio.sleep(2.0)
                    if await self._mongodb_ping():
                        log.info(
                            "mongodb port open; ping recovered without restart"
                        )
                        return True
                log.warning(
                    "mongodb port %s:%s open but ping still fails; "
                    "skipping brew restart",
                    host,
                    port,
                )
                return False

        for cmd in commands:
            try:
                log.info("mongodb restart: trying %s", " ".join(cmd))
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=cmd_timeout
                )
                if proc.returncode == 0:
                    log.info("mongodb restart command succeeded: %s", " ".join(cmd))
                    deadline = time.monotonic() + mkc.startup_wait_seconds
                    while time.monotonic() < deadline:
                        await asyncio.sleep(2.0)
                        if await self._mongodb_ping():
                            return True
                    log.warning(
                        "mongodb restart command exited 0 but ping still fails"
                    )
                else:
                    err = (stderr or b"").decode("utf-8", errors="replace").strip()
                    log.warning(
                        "mongodb restart command failed (rc=%d): %s — %s",
                        proc.returncode,
                        " ".join(cmd),
                        err[:200],
                    )
            except asyncio.TimeoutError:
                log.warning(
                    "mongodb restart command timed out after %.0fs: %s",
                    cmd_timeout,
                    " ".join(cmd),
                )
            except FileNotFoundError:
                log.debug("mongodb restart command not found: %s", cmd[0])
            except Exception as exc:
                log.warning("mongodb restart command error: %s — %s", " ".join(cmd), exc)

        ok = await asyncio.to_thread(restart_mongod)
        if ok and await self._mongodb_ping():
            return True
        return False
