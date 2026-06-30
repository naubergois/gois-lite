"""WhatsApp, alerting, skill-discovery loops and webhook URL helper."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .skill_discovery import discover_skill_suggestions
from .wacli_auth import resolve_wacli_bin
from .wacli_sync import (
    list_wacli_processes,
    pause_webhook_sync_for_send,
    terminate_blocking_wacli_sync,
)
from .whatsapp_digest import format_status_digest
from .whatsapp_outbound import enqueue_whatsapp

log = logging.getLogger(__name__)

_HERMES_LOG_PREFIX = ".hermes/"


class MonitorBackgroundLoopsMixin:
    def _whatsapp_webhook_url(self) -> str:
        host = self.cfg.http.host
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"http://{host}:{self.cfg.http.port}/whatsapp/inbound"

    async def _whatsapp_wacli_sync_loop(self) -> None:
        """Keep ``wacli sync --webhook`` running for live inbound messages."""
        wd = self.cfg.whatsapp_digest
        webhook = self._whatsapp_webhook_url()
        listener_warned = False
        lock_failures = 0
        while True:
            if not wd.inbound_enabled or not wd.inbound_sync_enabled:
                await asyncio.sleep(30.0)
                continue
            if not listener_warned:
                for _pid, command in list_wacli_processes():
                    if "whatsapp-listener-daemon" in command:
                        log.warning(
                            "whatsapp-listener-daemon detectado com inbound_sync_enabled=true "
                            "— o sync --once do listener disputa lock com envios. "
                            "Pare o daemon ou defina inbound_sync_enabled: false."
                        )
                        listener_warned = True
                        break
            cmd = [resolve_wacli_bin(wd.wacli_bin)]
            if wd.wacli_store_dir and str(wd.wacli_store_dir).strip():
                cmd.extend(["--store", str(wd.wacli_store_dir).strip()])
            cmd.extend(
                [
                    "sync",
                    "--follow",
                    "--webhook",
                    webhook,
                    "--webhook-allow-private",
                    "--lock-wait",
                    "45s",
                    "--max-reconnect",
                    "0",
                ]
            )
            if wd.inbound_webhook_secret:
                cmd.extend(["--webhook-secret", wd.inbound_webhook_secret])
            terminate_blocking_wacli_sync()
            stale = pause_webhook_sync_for_send(grace_seconds=0.2)
            if stale:
                log.warning(
                    "replaced %d stale wacli webhook sync process(es) before restart",
                    len(stale),
                )
            log.info("starting wacli sync webhook → %s", webhook)
            started = time.monotonic()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._whatsapp_sync_proc = proc
                _stdout, stderr = await proc.communicate()
                self._whatsapp_sync_proc = None
                elapsed = time.monotonic() - started
                err = (stderr or b"").decode("utf-8", errors="replace").strip()
                log.warning(
                    "wacli sync exited code=%s after %.0fs: %s",
                    proc.returncode,
                    elapsed,
                    err[:400] or "(no stderr)",
                )
                lock_failures = await self._maybe_self_heal_wacli_sync(
                    wd,
                    returncode=proc.returncode,
                    err=err,
                    elapsed=elapsed,
                    lock_failures=lock_failures,
                )
            except Exception as e:
                log.warning("wacli sync failed to start: %s", e)
            await asyncio.sleep(15.0)

    async def _maybe_self_heal_wacli_sync(
        self,
        wd: Any,
        *,
        returncode: Any,
        err: str,
        elapsed: float,
        lock_failures: int,
    ) -> int:
        """Force-unlock the wacli store after repeated fast lock failures.

        Returns the updated consecutive lock-failure counter.
        """
        lower = err.lower()
        lock_trouble = (
            returncode not in (0, -15)
            and elapsed < 8.0
            and any(
                needle in lower
                for needle in ("lock", "busy", "another", "outra operação")
            )
        )
        if not lock_trouble:
            return 0
        lock_failures += 1
        if lock_failures < 3:
            return lock_failures
        log.warning(
            "wacli sync blocked on store lock %d× — forcing unlock to self-heal",
            lock_failures,
        )
        try:
            from .wacli_auth import wacli_unlock

            store_dir = (
                str(wd.wacli_store_dir).strip()
                if getattr(wd, "wacli_store_dir", None)
                else None
            )
            result = await asyncio.to_thread(
                wacli_unlock,
                bin_path=wd.wacli_bin,
                store_dir=store_dir,
            )
            log.info(
                "wacli sync self-heal unlock: released=%s terminated=%s",
                result.get("lock_released"),
                result.get("terminated_pids"),
            )
        except Exception as exc:
            log.warning("wacli sync self-heal unlock failed: %s", exc)
        return 0

    async def _whatsapp_digest_loop(self) -> None:
        wd = self.cfg.whatsapp_digest
        await asyncio.sleep(wd.interval_seconds)
        while True:
            if not self.tracker.get("whatsapp_digest").enabled:
                self.tracker.get("whatsapp_digest").state = "paused"
            else:
                try:
                    async with self.tracker.track("whatsapp_digest") as a:
                        snap = self.status_snapshot()
                        message = format_status_digest(snap)
                        wd_monitor = wd.model_copy(update={"skip_context_guard": True})
                        out = enqueue_whatsapp(wd_monitor, message, wait=False)
                        a.last_result = (
                            "enfileirado" if out.get("ok") else "falha ao enfileirar"
                        )
                except Exception as e:
                    log.exception("whatsapp digest crashed: %s", e)
            await asyncio.sleep(wd.interval_seconds)

    async def _alert_loop(self) -> None:
        """Evaluate alert rules periodically and dispatch notifications."""
        ac = self.cfg.alerting
        await asyncio.sleep(ac.interval_seconds)
        while True:
            if not self.tracker.get("alerting").enabled:
                self.tracker.get("alerting").state = "paused"
            else:
                try:
                    async with self.tracker.track("alerting") as a:
                        snap = self.status_snapshot()
                        from .whatsapp_outbound import enqueue_whatsapp

                        wd_monitor = self.cfg.whatsapp_digest.model_copy(
                            update={"skip_context_guard": True}
                        )

                        async def _send_whatsapp(msg: str) -> bool:
                            out = enqueue_whatsapp(
                                wd_monitor,
                                msg,
                                wait=True,
                                wait_timeout=float(wd_monitor.timeout_seconds) + 120.0,
                            )
                            return bool(out.get("send_ok"))

                        async def _send_notifier(level: str, title: str, body: str) -> None:
                            await self.notifier.notify(level, title, body)

                        fired = await self.alert_engine.evaluate(
                            snap,
                            send_whatsapp_fn=_send_whatsapp,
                            send_notifier_fn=_send_notifier,
                        )
                        count = len(fired)
                        if count:
                            a.last_result = f"{count} alerta(s) disparado(s)"
                        else:
                            a.last_result = "ok"
                except Exception as e:
                    log.exception("alert loop crashed: %s", e)
            await asyncio.sleep(ac.interval_seconds)

    async def _skill_discovery_loop(self) -> None:
        sd = self.cfg.skill_discovery
        await asyncio.sleep(sd.startup_delay_seconds)
        while True:
            if not self.tracker.get("skill_discovery").enabled:
                self.tracker.get("skill_discovery").state = "paused"
            else:
                try:
                    async with self.tracker.track("skill_discovery") as a:
                        agent = self.cfg.openclaw_chat.default_agent
                        result = await asyncio.to_thread(
                            discover_skill_suggestions, sd, agent_id=agent
                        )
                        new_n = int(result.get("new_count") or 0)
                        pending = int(result.get("pending_count") or 0)
                        a.last_result = f"new={new_n} pending={pending}"
                        if new_n:
                            self._maybe_notify_skill_suggestions(
                                result.get("new") or []
                            )
                except Exception as e:
                    log.exception("skill discovery crashed: %s", e)
            await asyncio.sleep(sd.interval_seconds)

    async def _handle_scan_result(self, result) -> None:
        self.state.last_log_scan_ts = result.ts
        self.state.last_log_matches = len(result.matches)
        self.state.last_log_match_details = [m.to_dict() for m in result.matches[-10:]]
        self.state.log_scanner_offsets = dict(self.log_scanner.offsets)
        if self.metrics:
            self.metrics.log_scans_total.inc()
            for m in result.matches:
                self.metrics.log_matches_total.labels(pattern=m.pattern).inc()
        if result.matches:
            top = result.matches[0]
            await self.notifier.notify(
                "warning",
                f"{self.cfg.qclaw.name} log scanner: {len(result.matches)} match(es)",
                "\n".join(f"[{m.pattern}] {m.file}: {m.line}" for m in result.matches[:5]),
            )
            # Auto-doctor runs *before* deciding to bounce the app. It's
            # non-destructive (repairs ~/.openclaw config) and frequently
            # resolves "Service connection error" without needing a restart.
            await self._maybe_run_openclaw_doctor(result.matches)
            await self._maybe_retry_hermes_cron(result.matches)
            # Route log-triggered recovery to the service that owns the log file.
            if self.cfg.log_scanner.trigger_recovery:
                for match in result.matches:
                    service = self._service_for_log_file(match.file)
                    if service == "hermes" and self.cfg.hermes:
                        kc = self.cfg.hermes_keepalive
                        urgent = (
                            kc.enabled
                            and match.pattern in kc.log_trigger_patterns
                        )
                        if urgent or self.state.hermes_consecutive_failures > 0:
                            await self._maybe_restart_hermes(
                                last_result={"log_match": match.to_dict()},
                                reason=(
                                    f"log pattern {match.pattern!r} matched in {match.file}"
                                    + (
                                        f" (health failing "
                                        f"{self.state.hermes_consecutive_failures}x)"
                                        if self.state.hermes_consecutive_failures > 0
                                        else " (urgent shutdown pattern)"
                                    )
                                ),
                                force=urgent,
                            )
                            break
                        log.info(
                            "log_scanner: hermes match %r but health OK — not restarting",
                            match.pattern,
                        )
                    elif service == "qclaw":
                        if self.state.consecutive_failures > 0:
                            await self._maybe_trigger_recovery(
                                last_result={"log_match": match.to_dict()},
                                reason=(
                                    f"log pattern {match.pattern!r} matched in {match.file} "
                                    f"(health failing {self.state.consecutive_failures}x)"
                                ),
                            )
                            break
                        log.info(
                            "log_scanner: %d match(es) but health is OK — not triggering recovery (top=%s)",
                            len(result.matches), match.pattern,
                        )
        self._persist()

    @staticmethod
    def _service_for_log_file(path: str) -> str:
        """Return 'hermes' for ~/.hermes logs, else 'qclaw'."""
        if _HERMES_LOG_PREFIX in path.replace("\\", "/"):
            return "hermes"
        return "qclaw"

    async def _reap_once(self) -> str:
        result = await self.reaper.reap()
        self.state.last_reap_ts = result.ts
        self.state.last_reap_zombies = len(result.zombies)
        self.state.last_reap_orphans = len(result.orphans)
        self.state.last_reap_killed = len(result.killed)
        self.state.last_reap_summary = result.summary()
        self.state.last_reap_details = result.killed[-20:] if result.killed else []
        if self.metrics:
            self.metrics.last_reap_ts.set(result.ts)
            self.metrics.last_reap_zombies.set(len(result.zombies))
            self.metrics.last_reap_orphans.set(len(result.orphans))
            if result.zombies:
                self.metrics.zombies_detected_total.inc(len(result.zombies))
            for k in result.killed:
                self.metrics.processes_reaped_total.labels(
                    kind=k.get("signal", "TERM").lower()
                ).inc()
        log.info(
            "reaper: scanned=%d main_alive=%s %s",
            result.scanned, result.main_alive, result.summary(),
        )
        if result.killed or result.zombies:
            level = "warning" if result.killed else "info"
            await self.notifier.notify(
                level,
                f"{self.cfg.qclaw.name} reaper: {result.summary()}",
                "\n".join(
                    f"{k['signal']:4} pid={k['pid']}  {k['command']}"
                    for k in result.killed
                ) or "(no kills)",
            )
        self._persist()
        return result.summary()

