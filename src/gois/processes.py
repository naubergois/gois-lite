"""Snapshot of the live QClaw process tree, with friendly names.

The agent panel shows what the monitor itself is doing; this complements
it with what the monitored *service* is actually doing on the host — the
main Electron process, each Helper, the bridge, the gateway, etc.

We keep a small ordered catalogue of regex → (friendly_name, role). The
first matching pattern wins, so list more specific patterns first.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass
from typing import List

from .reaper import _etime_to_seconds


@dataclass
class QclawProcess:
    pid: int
    ppid: int
    name: str         # friendly display name
    role: str         # main | helper | bridge | gateway | other
    command: str      # full argv (truncated)
    age_seconds: int
    rss_kb: int = 0
    vsz_kb: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# Order matters: first matching pattern wins.
_CATALOGUE: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"QClaw Helper \(GPU\)"),                  "Helper (GPU)",      "helper"),
    (re.compile(r"QClaw Helper \(Renderer\)"),             "Helper (Renderer)", "helper"),
    (re.compile(r"QClaw Helper.*utility-sub-type=network"), "Helper (Network)",  "helper"),
    (re.compile(r"QClaw Helper \(Plugin\)"),               "Helper (Plugin)",   "helper"),
    (re.compile(r"QClaw Helper(?!.*\()"),                  "Helper",            "helper"),
    (re.compile(r"chrome_crashpad_handler"),               "Crashpad reporter", "helper"),
    (re.compile(r"/Applications/QClaw\.app/Contents/MacOS/QClaw(?!\s*Helper)"),
                                                            "QClaw (main)",      "main"),
    (re.compile(r"whatsapp-bridge-daemon"),                "WhatsApp bridge",   "bridge"),
    (re.compile(r"openclaw-gateway"),                      "Openclaw gateway",  "gateway"),
    (re.compile(r"openclaw-agent"),                        "Openclaw agent",    "gateway"),
    (re.compile(r"\.qclaw-oversea/"),                      "QClaw workspace",   "other"),
    (re.compile(r"hermes.*dashboard|hermes_cli\.main.*dashboard"),
                                                            "Hermes dashboard",  "gateway"),
    (re.compile(r"hermes gateway run|hermes_cli\.main.*gateway run"),
                                                            "Hermes gateway",    "gateway"),
    (re.compile(r"(?:\.hermes/hermes-agent/|/vendor/hermes-agent/)"),
                                                            "Hermes agent",      "other"),
]


def classify(command: str) -> tuple[str, str] | None:
    """Return (friendly_name, role) for the command, or None if unrelated."""
    for pat, name, role in _CATALOGUE:
        if pat.search(command):
            return name, role
    return None


def parse_ps(output: str) -> List[QclawProcess]:
    rows: List[QclawProcess] = []
    lines = output.splitlines()
    if not lines:
        return rows
    for raw in lines[1:]:
        # Two supported shapes:
        #   legacy: pid ppid etime command…              (4 leading tokens)
        #   new   : pid ppid rss vsz etime command…      (6 leading tokens)
        # Telling them apart by `len(parts)` is unreliable because `command`
        # often contains spaces. Detect via the third token: a digit means RSS
        # (new shape); a `:` or `-` means etime (legacy shape).
        parts = raw.strip().split()
        if len(parts) < 4:
            continue
        if parts[2].isdigit():
            if len(parts) < 6:
                continue
            pid_s, ppid_s, rss_s, vsz_s, etime_s = parts[:5]
            command = " ".join(parts[5:])
        else:
            pid_s, ppid_s, etime_s = parts[:3]
            rss_s = vsz_s = "0"
            command = " ".join(parts[3:])
        try:
            pid, ppid = int(pid_s), int(ppid_s)
        except ValueError:
            continue
        classified = classify(command)
        if classified is None:
            continue
        name, role = classified
        try:
            rss = int(rss_s)
            vsz = int(vsz_s)
        except ValueError:
            rss = vsz = 0
        rows.append(
            QclawProcess(
                pid=pid,
                ppid=ppid,
                name=name,
                role=role,
                command=command[:200],
                age_seconds=_etime_to_seconds(etime_s),
                rss_kb=rss,
                vsz_kb=vsz,
            )
        )
    # main process(es) first, then helpers (sorted by RSS desc), then bridge/gateway, then other
    role_rank = {"main": 0, "helper": 1, "bridge": 2, "gateway": 3, "other": 4}
    rows.sort(key=lambda r: (role_rank.get(r.role, 99), -r.rss_kb, r.name, r.pid))
    return rows


def memory_summary(rows: List[QclawProcess]) -> dict:
    total = sum(r.rss_kb for r in rows) * 1024
    by_role: dict[str, int] = {}
    for r in rows:
        by_role[r.role] = by_role.get(r.role, 0) + r.rss_kb * 1024
    return {
        "total_bytes": total,
        "by_role_bytes": by_role,
        "process_count": len(rows),
    }


async def list_qclaw_processes() -> List[QclawProcess]:
    proc = await asyncio.create_subprocess_exec(
        "ps", "-axwwo", "pid,ppid,rss,vsz,etime,command",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    return parse_ps(out.decode(errors="replace"))
