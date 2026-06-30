"""Prometheus metrics + re-exports for the HTTP dashboard server."""

from __future__ import annotations

import logging

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from wsgiref.simple_server import make_server

log = logging.getLogger(__name__)

class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.health_checks_total = Counter(
            "qclaw_health_checks_total",
            "Total qclaw health checks performed.",
            ["result"],
            registry=self.registry,
        )
        self.consecutive_failures = Gauge(
            "qclaw_consecutive_failures",
            "Current consecutive health-check failures.",
            registry=self.registry,
        )
        self.recovery_attempts_total = Counter(
            "qclaw_recovery_attempts_total",
            "Times the recovery agent has been invoked.",
            registry=self.registry,
        )
        self.recovery_agent_errors_total = Counter(
            "qclaw_recovery_agent_errors_total",
            "Times the recovery agent crashed.",
            registry=self.registry,
        )
        self.last_recovery_ts = Gauge(
            "qclaw_last_recovery_timestamp_seconds",
            "Unix timestamp of the most recent recovery attempt.",
            registry=self.registry,
        )
        self.last_health_ok_ts = Gauge(
            "qclaw_last_health_ok_timestamp_seconds",
            "Unix timestamp of the most recent successful health check.",
            registry=self.registry,
        )
        self.up = Gauge(
            "qclaw_up",
            "1 if last health check succeeded else 0.",
            registry=self.registry,
        )
        self.hermes_consecutive_failures = Gauge(
            "hermes_consecutive_failures",
            "Current consecutive Hermes health-check failures.",
            registry=self.registry,
        )
        self.hermes_up = Gauge(
            "hermes_up",
            "1 if last Hermes health check succeeded else 0.",
            registry=self.registry,
        )
        self.processes_reaped_total = Counter(
            "qclaw_processes_reaped_total",
            "Processes the reaper killed, by kind.",
            ["kind"],
            registry=self.registry,
        )
        self.zombies_detected_total = Counter(
            "qclaw_zombies_detected_total",
            "Zombie (Z-state) processes the reaper has seen.",
            registry=self.registry,
        )
        self.last_reap_ts = Gauge(
            "qclaw_last_reap_timestamp_seconds",
            "Wall-clock of last reaper scan.",
            registry=self.registry,
        )
        self.last_reap_zombies = Gauge(
            "qclaw_last_reap_zombies",
            "Zombies seen at last reaper scan.",
            registry=self.registry,
        )
        self.last_reap_orphans = Gauge(
            "qclaw_last_reap_orphans",
            "Orphans seen at last reaper scan.",
            registry=self.registry,
        )
        self.memory_rss_bytes = Gauge(
            "qclaw_memory_rss_bytes",
            "Resident set size of QClaw-related processes, by role.",
            ["role"],
            registry=self.registry,
        )
        self.memory_rss_total_bytes = Gauge(
            "qclaw_memory_rss_total_bytes",
            "Total RSS across all QClaw-related processes.",
            registry=self.registry,
        )
        self.log_scans_total = Counter(
            "qclaw_log_scans_total",
            "Total log-scanner runs.",
            registry=self.registry,
        )
        self.log_matches_total = Counter(
            "qclaw_log_matches_total",
            "Log patterns matched, by pattern name.",
            ["pattern"],
            registry=self.registry,
        )
        # --- R2: Swarm performance metrics ---
        self.swarm_runs_total = Counter(
            "qclaw_swarm_runs_total",
            "Total swarm graph runs, by name and final status.",
            ["swarm_name", "status"],
            registry=self.registry,
        )
        self.swarm_node_duration_seconds = Gauge(
            "qclaw_swarm_node_duration_seconds",
            "Duration of the last node execution in seconds.",
            ["swarm_name", "node_name"],
            registry=self.registry,
        )
        self.swarm_errors_total = Counter(
            "qclaw_swarm_errors_total",
            "Total swarm node errors, by swarm and error type.",
            ["swarm_name", "error_type"],
            registry=self.registry,
        )
        self.swarm_active = Gauge(
            "qclaw_swarm_active",
            "Number of currently running swarm graphs.",
            registry=self.registry,
        )
        self.swarm_eval_score = Gauge(
            "qclaw_swarm_eval_score",
            "Last evaluation score (0-100) per swarm.",
            ["swarm_name"],
            registry=self.registry,
        )
        # --- HTTP latency (P2 observability) ---
        _latency_buckets = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 12.0, 20.0)
        self.ruflo_status_duration_seconds = Histogram(
            "qclaw_ruflo_status_duration_seconds",
            "Duration of /ruflo/status handler in seconds.",
            buckets=_latency_buckets,
            registry=self.registry,
        )
        self.swarm_robots_build_duration_seconds = Histogram(
            "qclaw_swarm_robots_build_duration_seconds",
            "Duration of /swarm/robots snapshot rebuild in seconds.",
            buckets=_latency_buckets,
            registry=self.registry,
        )
        self.ruflo_status_failures_total = Counter(
            "qclaw_ruflo_status_failures_total",
            "RuFlo status probes where swarm_ok is false or latency exceeded threshold.",
            registry=self.registry,
        )


from .metrics_http import (
    _gzip_etag_middleware,
    _wants_metrics_html,
    _wsgi_path_segment,
    run_http_server,
)

__all__ = [
    "Metrics",
    "make_server",
    "_gzip_etag_middleware",
    "_wants_metrics_html",
    "_wsgi_path_segment",
    "run_http_server",
]
