"""Aggregate error lines from monitor logs and persisted monitor state."""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import Config, ErrorLogConfig, LogPatternConfig
from .log_scanner import LogPattern
from .state import MonitorState

_TS_PATTERNS = (
    re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)"),
    re.compile(r"^(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})"),
)

_ERROR_LINE_RES = (
    re.compile(r"\bERROR\b", re.I),
    re.compile(r"\bCRITICAL\b", re.I),
    re.compile(r"\bFATAL\b", re.I),
    re.compile(r"\bException\b"),
    re.compile(r"\bTraceback\b"),
    re.compile(r"\bfailed\b", re.I),
    re.compile(r"Service connection error", re.I),
    re.compile(r"Exiting with code [1-9]", re.I),
)

_ERRORS_BASENAME = frozenset({"errors.log", "gateway.err.log", "stderr.log"})


@dataclass(frozen=True)
class ErrorEntry:
    ts_epoch: Optional[float]
    ts_label: Optional[str]
    source: str
    source_label: str
    category: str
    line: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts_label,
            "ts_epoch": self.ts_epoch,
            "source": self.source,
            "source_label": self.source_label,
            "category": self.category,
            "line": self.line,
        }


def _source_label(path: Path) -> str:
    parts = path.parts
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return path.name


def _parse_ts(line: str) -> tuple[Optional[float], Optional[str]]:
    for pat in _TS_PATTERNS:
        match = pat.match(line)
        if not match:
            continue
        raw = match.group(1).replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
            try:
                dt = datetime.strptime(raw[:26], fmt[: len(raw)])
                return dt.timestamp(), raw
            except ValueError:
                continue
    return None, None


def _compile_scanner_patterns(cfg: Config) -> list[LogPattern]:
    out: list[LogPattern] = []
    for item in cfg.log_scanner.patterns:
        if isinstance(item, LogPatternConfig):
            out.append(LogPattern(name=item.name, pattern=item.pattern))
    return out


def resolve_error_log_paths(cfg: Config) -> list[Path]:
    """Collect unique log file paths to scan for errors."""
    seen: set[str] = set()
    paths: list[Path] = []

    def add(raw: str) -> None:
        p = str(Path(raw).expanduser().resolve())
        if p in seen:
            return
        seen.add(p)
        paths.append(Path(p))

    el = cfg.error_log
    for raw in el.extra_paths:
        add(raw)
    for raw in cfg.log_scanner.paths:
        add(raw)
    if cfg.hermes:
        for raw in cfg.hermes.log_paths:
            add(raw)
    if cfg.notifier.log_file:
        add(cfg.notifier.log_file)
    return paths


def _read_tail_lines(path: Path, max_lines: int) -> list[str]:
    if not path.is_file():
        return []
    try:
        with path.open("r", errors="replace") as handle:
            return list(deque(handle, maxlen=max(1, max_lines)))
    except OSError:
        return []


def _line_is_error(
    line: str,
    *,
    path: Path,
    scanner_patterns: list[LogPattern],
) -> tuple[bool, str]:
    stripped = line.strip()
    if not stripped:
        return False, ""
    if path.name in _ERRORS_BASENAME:
        return True, "errors_log"
    for pat in scanner_patterns:
        if pat.search(stripped):
            return True, pat.name
    for rx in _ERROR_LINE_RES:
        if rx.search(stripped):
            return True, "error_line"
    return False, ""


def _scan_file(
    path: Path,
    *,
    max_lines: int,
    scanner_patterns: list[LogPattern],
) -> list[ErrorEntry]:
    rows: list[ErrorEntry] = []
    mtime = path.stat().st_mtime if path.is_file() else time.time()
    for line in _read_tail_lines(path, max_lines):
        ok, category = _line_is_error(line, path=path, scanner_patterns=scanner_patterns)
        if not ok:
            continue
        ts_epoch, ts_label = _parse_ts(line)
        if ts_epoch is None:
            ts_epoch = mtime
        rows.append(
            ErrorEntry(
                ts_epoch=ts_epoch,
                ts_label=ts_label,
                source=str(path),
                source_label=_source_label(path),
                category=category,
                line=line.strip()[:2000],
            )
        )
    return rows


def _monitor_events(cfg: Config, state: MonitorState) -> list[dict[str, Any]]:
    if not cfg.error_log.include_monitor_events:
        return []
    events: list[dict[str, Any]] = []
    now = time.time()

    def add(category: str, message: str, *, ts: Optional[float] = None) -> None:
        text = (message or "").strip()
        if not text:
            return
        events.append(
            {
                "ts": ts,
                "ts_epoch": ts or now,
                "category": category,
                "line": text[:2000],
                "source": "monitor",
                "source_label": "monitor",
            }
        )

    if state.last_failure_summary:
        add("qclaw_health", state.last_failure_summary, ts=state.last_health_fail_ts)
    if state.last_recovery_report:
        add("qclaw_recovery", state.last_recovery_report, ts=state.last_recovery_ts)
    if state.hermes_last_failure_summary:
        add(
            "hermes_health",
            state.hermes_last_failure_summary,
            ts=state.hermes_last_health_fail_ts,
        )
    if state.hermes_last_recovery_report:
        add(
            "hermes_recovery",
            state.hermes_last_recovery_report,
            ts=state.hermes_last_recovery_ts,
        )
    if state.last_doctor_summary and state.last_doctor_ok is False:
        add("openclaw_doctor", state.last_doctor_summary, ts=state.last_doctor_ts)
    if state.last_hermes_cron_summary and state.last_hermes_cron_retry_ok is False:
        add("hermes_cron", state.last_hermes_cron_summary, ts=state.last_hermes_cron_retry_ts)
    if state.last_hermes_cron_gateway_summary and state.last_hermes_cron_gateway_ok is False:
        add(
            "hermes_cron_gateway",
            state.last_hermes_cron_gateway_summary,
            ts=state.last_hermes_cron_gateway_ensure_ts,
        )
    for detail in state.last_log_match_details or []:
        if not isinstance(detail, dict):
            continue
        line = str(detail.get("line") or "").strip()
        if not line:
            continue
        events.append(
            {
                "ts_epoch": float(detail.get("ts") or now),
                "ts": None,
                "category": str(detail.get("pattern") or "log_scanner"),
                "line": line[:2000],
                "source": str(detail.get("file") or "log_scanner"),
                "source_label": _source_label(Path(str(detail.get("file") or "log_scanner"))),
            }
        )
    for detail in state.last_reap_details or []:
        if not isinstance(detail, dict):
            continue
        pid = detail.get("pid")
        cmd = detail.get("command") or detail.get("cmd") or ""
        add("reaper", f"pid {pid}: {cmd}".strip(), ts=state.last_reap_ts)
    return events


def collect_errors(
    cfg: Config,
    state: MonitorState,
    *,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """Return aggregated errors from log files and monitor state."""
    el = cfg.error_log
    if not el.enabled:
        return {"ok": False, "error": "error log is disabled"}

    cap = int(limit if limit is not None else el.max_errors)
    cap = max(1, min(cap, 2000))
    scanner_patterns = _compile_scanner_patterns(cfg)
    paths = resolve_error_log_paths(cfg)

    rows: list[ErrorEntry] = []
    missing: list[str] = []
    for path in paths:
        if not path.is_file():
            missing.append(str(path))
            continue
        rows.extend(
            _scan_file(
                path,
                max_lines=el.tail_lines_per_file,
                scanner_patterns=scanner_patterns,
            )
        )

    monitor_events = _monitor_events(cfg, state)
    file_errors = [r.to_dict() for r in rows]
    combined = monitor_events + file_errors
    combined.sort(key=lambda item: float(item.get("ts_epoch") or 0), reverse=True)
    combined = combined[:cap]

    sources = [{"path": str(p), "exists": p.is_file()} for p in paths]
    return {
        "ok": True,
        "generated_at": time.time(),
        "summary": {
            "total": len(combined),
            "from_logs": len(file_errors),
            "monitor_events": len(monitor_events),
            "sources_scanned": sum(1 for p in paths if p.is_file()),
            "sources_missing": len(missing),
        },
        "sources": sources,
        "missing_paths": missing[:20],
        "monitor_events": monitor_events,
        "errors": combined,
    }
