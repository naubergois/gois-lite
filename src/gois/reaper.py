"""Periodic zombie / orphan reaper for the QClaw process tree.

Scans the host process table with `ps` and decides:

  - **Zombies** (state contains "Z" and command matches target_pattern):
    reported and counted, but never killed directly. The only way to clear
    a true zombie is to make its parent call wait(), so we nudge the parent
    with SIGCHLD; if the parent itself is dead, launchd reparents and reaps.

  - **Orphans** (command matches target_pattern, main qclaw process is
    absent): SIGTERM, then SIGKILL after `sigterm_grace_seconds`.

`ps_runner` and `killer` are injectable for unit tests.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

log = logging.getLogger(__name__)

PS_FIELDS = ("pid", "ppid", "stat", "etime", "command")
# openclaw helpers that should live under the main QClaw process tree.
_STALE_GATEWAY = re.compile(r"openclaw-(gateway|agent)")
# Hermes gateway workers that can survive as ppid=1 orphans.
_STALE_HERMES = re.compile(r"hermes gateway run|hermes_cli\.main gateway")
# macOS QClaw.app main binary — never reap even if process_pattern in config is wrong.
_QCLAW_MAIN_BINARY = "/Applications/QClaw.app/Contents/MacOS/QClaw"


def _is_qclaw_main_process(command: str, main_marker: str) -> bool:
    if _QCLAW_MAIN_BINARY in command:
        return True
    return bool(main_marker and main_marker in command)


def _is_openclaw_gateway_process(command: str) -> bool:
    return "openclaw-gateway" in command


def _reap_stale_openclaw_gateways(gateways: List[PsRow]) -> List[PsRow]:
    """Keep the newest gateway; return older duplicates to reap."""
    if len(gateways) <= 1:
        return []
    gateways.sort(key=lambda row: row.etimes)
    return gateways[1:]


def _etime_to_seconds(s: str) -> int:
    """Parse BSD `ps -o etime` (eg. '00:42', '01:23:45', '3-04:05:06') → seconds.
    Bare integers (eg. '600') are accepted too — handy for tests and for hosts
    that happen to expose `etimes`."""
    if ":" not in s and "-" not in s:
        try:
            return int(s)
        except ValueError:
            return 0
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [int(p) for p in s.split(":")]
    if len(parts) == 2:
        h, m, sec = 0, parts[0], parts[1]
    elif len(parts) == 3:
        h, m, sec = parts
    else:
        return 0
    return days * 86400 + h * 3600 + m * 60 + sec
PsRunner = Callable[[], Awaitable[str]]
Killer = Callable[[int, int], None]


@dataclass
class PsRow:
    pid: int
    ppid: int
    stat: str
    etimes: int  # elapsed seconds since process start
    command: str

    @property
    def is_zombie(self) -> bool:
        return "Z" in self.stat


@dataclass
class ReapResult:
    ts: float = field(default_factory=time.time)
    scanned: int = 0
    main_alive: bool = False
    zombies: List[PsRow] = field(default_factory=list)
    orphans: List[PsRow] = field(default_factory=list)
    killed: List[dict] = field(default_factory=list)

    def summary(self) -> str:
        parts: list[str] = []
        if self.zombies:
            parts.append(f"{len(self.zombies)} zombie(s)")
        if self.orphans:
            parts.append(f"{len(self.orphans)} orphan(s)")
        if self.killed:
            parts.append(f"{len(self.killed)} killed")
        if not parts:
            parts.append("clean")
        return ", ".join(parts)

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "scanned": self.scanned,
            "main_alive": self.main_alive,
            "zombies": [r.__dict__ for r in self.zombies],
            "orphans": [r.__dict__ for r in self.orphans],
            "killed": self.killed,
            "summary": self.summary(),
        }


class Reaper:
    def __init__(
        self,
        qcfg: QclawConfig,
        rcfg: ReaperConfig,
        *,
        hcfg: Optional[QclawConfig] = None,
        ps_runner: Optional[PsRunner] = None,
        killer: Optional[Killer] = None,
    ):
        self.qcfg = qcfg
        self.hcfg = hcfg
        self.rcfg = rcfg
        self._target = re.compile(rcfg.target_pattern)
        self._ps_runner = ps_runner or self._default_ps
        self._killer = killer or os.kill
        # the path/string we use to identify each service's main process
        self._main_marker = qcfg.process_pattern or qcfg.name
        self._hermes_marker = (
            (hcfg.process_pattern or hcfg.name) if hcfg else None
        )

    # ---------- io ----------

    async def _default_ps(self) -> str:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-axwwo", ",".join(PS_FIELDS),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"ps failed rc={proc.returncode}: {err.decode(errors='replace')}"
            )
        return out.decode(errors="replace")

    # ---------- pure logic (easy to test) ----------

    @staticmethod
    def _tree_pids(rows: List[PsRow], root: int) -> set[int]:
        """All PIDs reachable from *root* via parent→child links."""
        by_ppid: dict[int, list[int]] = {}
        for r in rows:
            by_ppid.setdefault(r.ppid, []).append(r.pid)
        tree = {root}
        stack = [root]
        while stack:
            parent = stack.pop()
            for child in by_ppid.get(parent, []):
                if child not in tree:
                    tree.add(child)
                    stack.append(child)
        return tree

    @staticmethod
    def parse_ps(output: str) -> List[PsRow]:
        lines = output.splitlines()
        if not lines:
            return []
        # skip header
        rows: List[PsRow] = []
        for raw in lines[1:]:
            parts = raw.strip().split(None, 4)
            if len(parts) < 5:
                continue
            pid_s, ppid_s, stat, etime_s, command = parts
            try:
                rows.append(
                    PsRow(
                        pid=int(pid_s),
                        ppid=int(ppid_s),
                        stat=stat,
                        etimes=_etime_to_seconds(etime_s),
                        command=command,
                    )
                )
            except ValueError:
                continue
        return rows

    def classify(self, rows: List[PsRow]) -> tuple[Optional[PsRow], List[PsRow], List[PsRow]]:
        main: Optional[PsRow] = None
        hermes_main: Optional[PsRow] = None
        matching: List[PsRow] = []
        zombies: List[PsRow] = []
        for r in rows:
            if self._main_marker and self._main_marker in r.command and not r.is_zombie:
                if main is None:
                    main = r
            if self._hermes_marker and self._hermes_marker in r.command and not r.is_zombie:
                if hermes_main is None:
                    hermes_main = r
            if self._target.search(r.command):
                matching.append(r)
            if r.is_zombie and self._target.search(r.command):
                zombies.append(r)
        orphans: List[PsRow] = []
        if main is None and self.rcfg.kill_orphan_helpers:
            stale_gateways: List[PsRow] = []
            for r in rows:
                if r.is_zombie:
                    continue
                if r.etimes < self.rcfg.min_age_seconds:
                    continue
                if _is_openclaw_gateway_process(r.command):
                    stale_gateways.append(r)
            orphans.extend(_reap_stale_openclaw_gateways(stale_gateways))
            for r in matching:
                if r.is_zombie:
                    continue
                if r.etimes < self.rcfg.min_age_seconds:
                    continue
                # Never reap the main QClaw binary — classify can miss it briefly at startup.
                if _is_qclaw_main_process(r.command, self._main_marker):
                    continue
                # When Hermes is configured, don't reap Hermes procs as QClaw orphans.
                if self._hermes_marker and self._hermes_marker in r.command:
                    continue
                if _is_openclaw_gateway_process(r.command):
                    continue
                orphans.append(r)
        elif main is not None and self.rcfg.kill_orphan_helpers:
            # Main is alive but openclaw-gateway/agent can survive as ppid=1
            # orphans after a crash/restart — they leak memory and fight the
            # live gateway for ports. Kill duplicate stale ones outside main's
            # tree.
            #
            # On macOS the live openclaw-gateway is often reparented to launchd
            # (ppid=1) alongside QClaw itself, so a lone gateway must never be
            # reaped while main is up — that breaks QClaw startup.
            live = self._tree_pids(rows, main.pid)
            stale_gateways: List[PsRow] = []
            for r in rows:
                if r.pid in live or r.is_zombie:
                    continue
                if not _STALE_GATEWAY.search(r.command):
                    continue
                if r.etimes < self.rcfg.min_age_seconds:
                    continue
                if _is_openclaw_gateway_process(r.command):
                    stale_gateways.append(r)
                    continue
                orphans.append(r)
            orphans.extend(_reap_stale_openclaw_gateways(stale_gateways))
        if self.rcfg.kill_orphan_helpers:
            from .roteiro_viral_local.orphan_cleanup import collect_rv_orphan_rows

            live_tree = self._tree_pids(rows, main.pid) if main is not None else set()
            rv_orphans = collect_rv_orphan_rows(
                rows,
                live_pids=live_tree,
                min_age_seconds=self.rcfg.min_age_seconds,
            )
            for row in rv_orphans:
                if row not in orphans:
                    orphans.append(row)
        # Hermes gateway orphans — independent of QClaw main.
        if self._hermes_marker and self.rcfg.kill_orphan_helpers:
            if hermes_main is None:
                for r in matching:
                    if r.is_zombie:
                        continue
                    if self._hermes_marker not in r.command:
                        continue
                    if r.etimes < self.rcfg.min_age_seconds:
                        continue
                    if r not in orphans:
                        orphans.append(r)
            elif hermes_main is not None:
                live = self._tree_pids(rows, hermes_main.pid)
                for r in rows:
                    if r.pid in live or r.is_zombie:
                        continue
                    if not _STALE_HERMES.search(r.command):
                        continue
                    if r.etimes < self.rcfg.min_age_seconds:
                        continue
                    if r not in orphans:
                        orphans.append(r)
        return main, zombies, orphans

    # ---------- action ----------

    async def reap(self) -> ReapResult:
        try:
            output = await self._ps_runner()
        except Exception as e:
            log.warning("reaper: ps failed: %s", e)
            return ReapResult()
        rows = self.parse_ps(output)
        main, zombies, orphans = self.classify(rows)
        result = ReapResult(
            scanned=len(rows),
            main_alive=main is not None,
            zombies=zombies,
            orphans=orphans,
        )

        # Nudge zombie parents (they should call wait()).
        for z in zombies:
            try:
                self._killer(z.ppid, signal.SIGCHLD)
            except (ProcessLookupError, PermissionError):
                pass

        # SIGTERM all orphan candidates.
        terminated: list[PsRow] = []
        for o in orphans:
            try:
                self._killer(o.pid, signal.SIGTERM)
                terminated.append(o)
                result.killed.append(
                    {"pid": o.pid, "signal": "TERM", "command": o.command[:140]}
                )
            except ProcessLookupError:
                pass
            except PermissionError as e:
                log.info("reaper: cannot signal pid %d: %s", o.pid, e)

        # Grace, then SIGKILL anything still alive among the orphans.
        if terminated and self.rcfg.sigterm_grace_seconds > 0:
            await asyncio.sleep(self.rcfg.sigterm_grace_seconds)
            try:
                recheck = self.parse_ps(await self._ps_runner())
                alive = {r.pid for r in recheck}
                for o in terminated:
                    if o.pid in alive:
                        try:
                            self._killer(o.pid, signal.SIGKILL)
                            result.killed.append(
                                {"pid": o.pid, "signal": "KILL", "command": o.command[:140]}
                            )
                        except (ProcessLookupError, PermissionError):
                            pass
            except Exception as e:
                log.warning("reaper: recheck failed: %s", e)

        return result
