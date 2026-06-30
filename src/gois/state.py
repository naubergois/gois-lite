"""Persistent monitor state.

Only counters that should survive restarts live here. `recovery_attempts` is
intentionally NOT persisted — it's a per-process safety cap that should reset
when an operator restarts the monitor (typically after applying a fix).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .runtime_state import load_json, save_json

log = logging.getLogger(__name__)

_MONITOR_STATE_KEY = "monitor:state"


@dataclass
class MonitorState:
    consecutive_failures: int = 0
    last_recovery_ts: Optional[float] = None        # wall-clock epoch
    last_health_ok_ts: Optional[float] = None
    last_health_fail_ts: Optional[float] = None
    last_recovery_report: Optional[str] = None
    last_failure_summary: Optional[str] = None
    # reaper
    last_reap_ts: Optional[float] = None
    last_reap_zombies: int = 0
    last_reap_orphans: int = 0
    last_reap_killed: int = 0
    last_reap_summary: Optional[str] = None
    last_reap_details: Optional[list] = None        # list of {pid, signal, command}
    # log scanner
    last_log_scan_ts: Optional[float] = None
    last_log_matches: int = 0
    last_log_match_details: Optional[list] = None   # list of {file, pattern, line, ts}
    log_scanner_offsets: Optional[dict] = None      # path → byte offset
    # openclaw doctor
    last_doctor_ts: Optional[float] = None
    last_doctor_ok: Optional[bool] = None
    last_doctor_trigger: Optional[str] = None       # pattern name that fired it
    last_doctor_summary: Optional[str] = None
    # hermes gateway (when cfg.hermes is set)
    hermes_consecutive_failures: int = 0
    hermes_last_recovery_ts: Optional[float] = None
    hermes_last_health_ok_ts: Optional[float] = None
    hermes_last_health_fail_ts: Optional[float] = None
    hermes_last_recovery_report: Optional[str] = None
    hermes_last_failure_summary: Optional[str] = None
    # hermes cron auto-retry
    last_hermes_cron_retry_ts: Optional[float] = None
    last_hermes_cron_retry_ok: Optional[bool] = None
    last_hermes_cron_job_id: Optional[str] = None
    last_hermes_cron_job_name: Optional[str] = None
    last_hermes_cron_summary: Optional[str] = None
    last_hermes_cron_gateway_ensure_ts: Optional[float] = None
    last_hermes_cron_gateway_ok: Optional[bool] = None
    last_hermes_cron_gateway_summary: Optional[str] = None
    last_hermes_cron_scheduler_recovery_ts: Optional[float] = None
    last_hermes_cron_scheduler_recovery_ok: Optional[bool] = None
    last_hermes_cron_scheduler_recovery_report: Optional[str] = None
    last_hermes_cron_diagnostic_ts: Optional[float] = None
    last_hermes_cron_diagnostic_ok: Optional[bool] = None
    last_hermes_cron_diagnostic_report: Optional[str] = None
    last_hermes_cron_diagnostic_healthy: Optional[bool] = None
    # whatsapp inbound auto-reply failures
    whatsapp_inbound_failures: int = 0
    whatsapp_inbound_last_error: Optional[str] = None
    whatsapp_inbound_last_failure_ts: Optional[float] = None
    # per-model daily quotas for Hermes cron usage
    model_daily_token_quotas: Optional[dict] = None  # model_id -> int tokens/day
    model_daily_usd_quotas: Optional[dict] = None    # model_id -> float USD/day
    model_usd_per_1k_prices: Optional[dict] = None   # model_id -> float USD per 1k tokens
    model_daily_quota_block: Optional[dict] = None   # {date, paused_job_ids, ...}
    # mongodb keepalive
    mongodb_consecutive_failures: int = 0
    mongodb_last_restart_ts: Optional[float] = None
    mongodb_last_restart_ok: Optional[bool] = None
    mongodb_last_restart_summary: Optional[str] = None

    @classmethod
    def load(cls, path: Optional[Path]) -> "MonitorState":
        data = load_json(_MONITOR_STATE_KEY, path)
        if data is None:
            return cls()
        try:
            if not isinstance(data, dict):
                return cls()
            defaults = cls()
            kwargs = {}
            for k in cls.__dataclass_fields__:
                val = data.get(k, getattr(defaults, k))
                if val is None and isinstance(getattr(defaults, k), int):
                    val = getattr(defaults, k)
                kwargs[k] = val
            return cls(**kwargs)
        except Exception as e:
            log.warning("could not load state from redis/file (%s); starting fresh", e)
            return cls()

    def save(self, path: Optional[Path]) -> None:
        if not path and not _MONITOR_STATE_KEY:
            return
        try:
            save_json(_MONITOR_STATE_KEY, asdict(self), path)
        except Exception as e:
            log.warning("could not save state: %s", e)
