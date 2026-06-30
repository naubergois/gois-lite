"""Leader election integration for mutating background loops."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from .leader_election import MonitorLeaderLock, monitor_instance_id
from .local_paths import project_stack_root

log = logging.getLogger(__name__)


class MonitorLeaderMixin:
    def init_leader_lock(self) -> None:
        le = self.cfg.monitor.leader_election
        stack = project_stack_root().expanduser().resolve()
        lock_path = stack / le.file_lock_name
        self._leader_lock = MonitorLeaderLock(
            enabled=le.enabled,
            instance_id=monitor_instance_id(),
            lock_key_suffix=le.lock_key,
            lease_seconds=le.lease_seconds,
            file_lock_path=lock_path,
        )
        self._leader_renew_task: asyncio.Task | None = None
        if le.enabled and hasattr(self, "tracker"):
            self.tracker.register(
                "leader",
                f"renew leader lease every {int(le.renew_interval_seconds)}s "
                f"({le.lock_key})",
            )

    def _leader_active(self) -> bool:
        return self._leader_lock.is_leader

    def _leader_status_snapshot(self) -> dict:
        return self._leader_lock.snapshot()

    async def _leader_startup(self) -> None:
        le = self.cfg.monitor.leader_election
        if not le.enabled:
            return
        acquired = self._leader_lock.try_acquire()
        if acquired:
            log.info("this instance is the monitor leader (%s)", self._leader_lock.instance_id)
        else:
            snap = self._leader_lock.snapshot()
            log.warning(
                "monitor leader held by %r — background recovery loops stay in standby",
                snap.get("holder"),
            )
        if self._leader_renew_task is None:
            self._leader_renew_task = asyncio.create_task(
                self._leader_renew_loop(),
                name="leader-renew",
            )

    async def _leader_shutdown(self) -> None:
        task = self._leader_renew_task
        self._leader_renew_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if getattr(self, "_leader_lock", None) is not None:
            self._leader_lock.release()

    async def _leader_renew_loop(self) -> None:
        le = self.cfg.monitor.leader_election
        agent = self.tracker.get("leader") if hasattr(self, "tracker") else None
        while True:
            await asyncio.sleep(le.renew_interval_seconds)
            if not le.enabled:
                return
            if agent is not None:
                async with self.tracker.track("leader") as ctx:
                    if self._leader_lock.is_leader:
                        ok = self._leader_lock.renew()
                        if not ok:
                            ok = self._leader_lock.try_acquire()
                        ctx.last_result = "leader" if ok else "lost lease"
                    else:
                        ok = self._leader_lock.try_acquire()
                        ctx.last_result = "acquired" if ok else "standby"
            else:
                if self._leader_lock.is_leader:
                    if not self._leader_lock.renew():
                        self._leader_lock.try_acquire()
                else:
                    self._leader_lock.try_acquire()

    async def _leader_standby_sleep(self) -> None:
        await asyncio.sleep(self.cfg.monitor.leader_election.renew_interval_seconds)

    def _spawn_leader_loop(
        self,
        name: str,
        loop_fn: Callable[[], Awaitable[None]],
    ) -> asyncio.Task:
        """Run a long-lived loop only while this instance holds leadership."""

        async def supervised() -> None:
            while True:
                if not self._leader_active():
                    agent = self.tracker.get(name)
                    if agent.enabled and agent.state != "paused":
                        agent.state = "standby"
                    await self._leader_standby_sleep()
                    continue
                worker = asyncio.create_task(loop_fn(), name=f"{name}-worker")
                poll = self.cfg.monitor.leader_election.renew_interval_seconds
                try:
                    while not worker.done():
                        try:
                            await asyncio.wait_for(asyncio.shield(worker), timeout=poll)
                        except asyncio.TimeoutError:
                            if not self._leader_active():
                                worker.cancel()
                                try:
                                    await worker
                                except asyncio.CancelledError:
                                    pass
                                break
                    if worker.done() and not worker.cancelled():
                        exc = worker.exception()
                        if exc is not None:
                            raise exc
                except asyncio.CancelledError:
                    worker.cancel()
                    raise

        return self._spawn_background_loop(name, supervised)
