"""Real-time alert engine — evaluates rules every health cycle.

Dispatches critical-event notifications (WhatsApp / webhook / log) when
conditions like consecutive failures, log-scanner matches, or threshold
breaches are met. Rate-limited per rule to avoid spam.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

from .config import AlertingConfig, AlertRuleConfig

log = logging.getLogger(__name__)

# Condition pattern: "consecutive_failures >= N"
_RE_CONDITION_FAILURES = re.compile(
    r"consecutive_failures\s*(>=?|==?|<=?)\s*(\d+)", re.IGNORECASE
)
_RE_REDIS_WRITE_FAILURES = re.compile(
    r"redis_write_failures\s*(>=?|==?|<=?)\s*(\d+)", re.IGNORECASE
)
_RE_RUFLO_LATENCY = re.compile(
    r"ruflo_latency_ms\s*(>=?|==?|<=?)\s*(\d+)", re.IGNORECASE
)
# Condition: "pattern_matched" — fires when log_scanner finds matches
_COND_PATTERN_MATCHED = "pattern_matched"
# Condition: "any_failure" — fires when consecutive_failures > 0
_COND_ANY_FAILURE = "any_failure"
# Condition: "data_integrity_degraded" — runtime Redis/file divergence
_COND_DATA_INTEGRITY_DEGRADED = "data_integrity_degraded"


class AlertRecord:
    """One alert that was raised — used for state tracking and rate limiting."""

    __slots__ = ("rule_name", "ts", "message")

    def __init__(self, rule_name: str, ts: float, message: str) -> None:
        self.rule_name = rule_name
        self.ts = ts
        self.message = message


class AlertEngine:
    """Evaluates alert rules against a status snapshot and dispatches notifications.

    Thread-safe for read/write of its internal state (GIL-protected). The caller
    (monitor health loop) passes a status snapshot and a dispatch callback.
    """

    def __init__(self, cfg: AlertingConfig, whatsapp_recipient: Optional[str] = None) -> None:
        self.cfg = cfg
        self.whatsapp_recipient = whatsapp_recipient
        # state: rule_name -> last_alert_ts
        self._last_alert_ts: dict[str, float] = {}
        # state: rule_name -> list of recent alert timestamps (for rate limiting)
        self._recent_alerts: dict[str, list[float]] = {}
        # state: rule_name -> last_message
        self._last_messages: dict[str, str] = {}
        # accumulated alert history for snapshot
        self._history: list[dict[str, Any]] = []

    def _eval_condition(
        self, rule: AlertRuleConfig, snap: dict[str, Any]
    ) -> Optional[str]:
        """Evaluate a single rule against the snapshot. Returns message or None."""
        source_key = rule.source
        source = snap.get(source_key, snap)

        failures = int(source.get("consecutive_failures", 0))

        # --- consecutive_failures >= N ---
        m = _RE_CONDITION_FAILURES.match(rule.condition.strip())
        if m:
            op = m.group(1).strip()
            threshold = int(m.group(2))
            if op == ">=" or op == ">":
                sat = failures >= threshold if op == ">=" else failures > threshold
            elif op == "<=" or op == "<":
                sat = failures <= threshold if op == "<=" else failures < threshold
            elif op == "==" or op == "=":
                sat = failures == threshold
            else:
                # unkown operator — skip silently
                return None
            if sat:
                return (
                    f"🔴 *{rule.name}* — {rule.source} {failures} falhas consecutivas "
                    f"(threshold {threshold})"
                )
            return None

        # --- pattern_matched ---
        if rule.condition.strip().lower() == _COND_PATTERN_MATCHED:
            scanner = source.get("log_scanner", snap.get("log_scanner", {}))
            if scanner.get("last_matches", 0) > 0:
                details = (scanner.get("last_details") or [])
                top = details[0] if details else {}
                return (
                    f"🟡 *{rule.name}* — log scanner: {scanner['last_matches']} match(es)"
                    f" ({top.get('pattern','?')} em {top.get('file','?')})"
                )
            return None

        # --- any_failure ---
        if rule.condition.strip().lower() == _COND_ANY_FAILURE:
            if failures > 0:
                return (
                    f"🔴 *{rule.name}* — {rule.source} com falha "
                    f"(consecutivas: {failures})"
                )
            return None

        # --- data_integrity_degraded ---
        if rule.condition.strip().lower() == _COND_DATA_INTEGRITY_DEGRADED:
            integrity = snap.get("data_integrity", {})
            if isinstance(integrity, dict) and integrity.get("degraded"):
                warnings = integrity.get("warnings") or []
                detail = warnings[0] if warnings else "runtime persistence degraded"
                return f"🟠 *{rule.name}* — integridade de dados: {detail}"
            return None

        # --- redis_write_failures >= N ---
        m_rw = _RE_REDIS_WRITE_FAILURES.match(rule.condition.strip())
        if m_rw:
            integrity = snap.get("data_integrity", snap.get("redis", {}))
            if source_key == "data_integrity" and isinstance(integrity, dict):
                rw_failures = int(integrity.get("redis_write_failures", 0))
            else:
                runtime = snap.get("redis", {}).get("runtime", {})
                rw_failures = int(runtime.get("redis_write_failures", 0))
            op = m_rw.group(1).strip()
            threshold = int(m_rw.group(2))
            if op in (">=", ">"):
                sat = rw_failures >= threshold if op == ">=" else rw_failures > threshold
            elif op in ("<=", "<"):
                sat = rw_failures <= threshold if op == "<=" else rw_failures < threshold
            elif op in ("==", "="):
                sat = rw_failures == threshold
            else:
                return None
            if sat:
                return (
                    f"🟠 *{rule.name}* — redis_write_failures={rw_failures} "
                    f"(threshold {threshold})"
                )
            return None

        # --- ruflo_latency_ms >= N ---
        m_lat = _RE_RUFLO_LATENCY.match(rule.condition.strip())
        if m_lat:
            latency = float(source.get("ruflo_latency_ms") or source.get("last_latency_ms") or 0)
            op = m_lat.group(1).strip()
            threshold = float(m_lat.group(2))
            if op in (">=", ">"):
                sat = latency >= threshold if op == ">=" else latency > threshold
            elif op in ("<=", "<"):
                sat = latency <= threshold if op == "<=" else latency < threshold
            elif op in ("==", "="):
                sat = latency == threshold
            else:
                return None
            if sat:
                return (
                    f"🟠 *{rule.name}* — RuFlo status lento ({latency:.0f}ms, "
                    f"threshold {threshold:.0f}ms)"
                )
            return None

        return None

    def _rate_limited(self, rule_name: str) -> bool:
        """Check if we've exceeded the per-rule-per-hour rate limit."""
        now = time.time()
        window = 3600.0
        max_per = self.cfg.max_per_hour_per_rule

        recent = self._recent_alerts.get(rule_name, [])
        # prune older than 1h
        recent = [t for t in recent if now - t < window]
        self._recent_alerts[rule_name] = recent

        if len(recent) >= max_per:
            return True
        return False

    def _should_fire(self, rule: AlertRuleConfig) -> bool:
        """Check cooldown: at least cooldown_seconds since last alert for this rule."""
        last = self._last_alert_ts.get(rule.name, 0.0)
        return (time.time() - last) >= rule.cooldown_seconds

    async def evaluate(
        self,
        snap: dict[str, Any],
        *,
        send_whatsapp_fn=None,
        send_notifier_fn=None,
    ) -> list[dict[str, Any]]:
        """Evaluate all rules against *snap*. Returns newly-fired alert dicts.

        Call from the health loop after building the status snapshot.
        *send_whatsapp_fn* — async callable (msg: str) -> bool
        *send_notifier_fn* — async callable (level: str, title: str, body: str)
        """
        if not self.cfg.enabled:
            return []

        fired: list[dict[str, Any]] = []

        for rule in self.cfg.rules:
            try:
                msg = self._eval_condition(rule, snap)
            except Exception as exc:
                log.warning("alert_eval %r crashed: %s", rule.name, exc)
                continue

            if msg is None:
                continue

            if not self._should_fire(rule):
                log.debug("alert %r: cooldown active — skipping", rule.name)
                continue

            if self._rate_limited(rule.name):
                log.info(
                    "alert %r: rate limit reached (max %d/h) — skipping",
                    rule.name,
                    self.cfg.max_per_hour_per_rule,
                )
                continue

            now = time.time()
            self._last_alert_ts[rule.name] = now
            recent = self._recent_alerts.setdefault(rule.name, [])
            recent.append(now)
            self._last_messages[rule.name] = msg

            entry: dict[str, Any] = {
                "rule": rule.name,
                "source": rule.source,
                "message": msg,
                "ts": now,
                "channel_whatsapp": False,
                "channel_webhook": False,
                "channel_log": False,
            }

            if rule.log:
                log.warning("ALERT [%s] %s", rule.name, msg)
                entry["channel_log"] = True

            if rule.webhook and send_notifier_fn:
                try:
                    await send_notifier_fn("warning", rule.name, msg)
                    entry["channel_webhook"] = True
                except Exception as exc:
                    log.warning("alert %r: webhook dispatch failed: %s", rule.name, exc)

            if rule.whatsapp and send_whatsapp_fn and self.whatsapp_recipient:
                try:
                    ok = await send_whatsapp_fn(msg)
                    entry["channel_whatsapp"] = bool(ok)
                    if not ok:
                        log.warning("alert %r: whatsapp dispatch returned false", rule.name)
                except Exception as exc:
                    log.warning("alert %r: whatsapp dispatch failed: %s", rule.name, exc)

            fired.append(entry)
            self._history.append(entry)

        if len(self._history) > 200:
            self._history = self._history[-200:]

        return fired

    def snapshot(self) -> dict[str, Any]:
        """Return current alert engine state for the dashboard."""
        now = time.time()
        active: list[dict[str, Any]] = []
        for d in self._history:
            age = now - d["ts"]
            if age < 3600:  # active if < 1h old
                active.append(dict(d))
            else:
                d_copy = dict(d)
                d_copy["cleared"] = True
                active.append(d_copy)

        return {
            "enabled": self.cfg.enabled,
            "rules_count": len(self.cfg.rules),
            "max_per_hour_per_rule": self.cfg.max_per_hour_per_rule,
            "active_alerts": len(active),
            "alerts": active[-30:],  # last 30 for dashboard
            "rate_limited_rules": [
                rule.name for rule in self.cfg.rules
                if self._rate_limited(rule.name)
            ],
        }

    def set_whatsapp_recipient(self, recipient: Optional[str]) -> None:
        """Update the WhatsApp recipient (may change at runtime)."""
        self.whatsapp_recipient = recipient

    def clear_history(self) -> None:
        """Clear alert history (e.g., after a manual reset)."""
        self._history.clear()
        self._last_alert_ts.clear()
        self._recent_alerts.clear()
        self._last_messages.clear()


def default_alert_rules() -> list[AlertRuleConfig]:
    """Return a sensible set of default alert rules."""
    return [
        AlertRuleConfig(
            name="qclaw-multiple-failures",
            source="qclaw",
            condition="consecutive_failures >= 3",
            cooldown_seconds=600.0,
            whatsapp=True,
            webhook=False,
            log=True,
            description="QClaw com 3+ falhas consecutivas de health check",
        ),
        AlertRuleConfig(
            name="hermes-down",
            source="hermes",
            condition="consecutive_failures >= 2",
            cooldown_seconds=600.0,
            whatsapp=True,
            webhook=False,
            log=True,
            description="Hermes gateway com 2+ falhas consecutivas",
        ),
        AlertRuleConfig(
            name="log-pattern-matched",
            source="log_scanner",
            condition="pattern_matched",
            cooldown_seconds=900.0,
            whatsapp=True,
            webhook=False,
            log=True,
            description="Log scanner encontrou padrões de erro",
        ),
        AlertRuleConfig(
            name="reaper-found-zombies",
            source="reaper",
            condition="any_failure",
            cooldown_seconds=1800.0,
            whatsapp=False,
            webhook=False,
            log=True,
            description="Reaper encontrou processos zumbis",
        ),
        AlertRuleConfig(
            name="hermes-cron-failed",
            source="hermes_cron_recovery",
            condition="any_failure",
            cooldown_seconds=900.0,
            whatsapp=True,
            webhook=False,
            log=True,
            description="Hermes cron recovery com falha",
        ),
        AlertRuleConfig(
            name="data-integrity-degraded",
            source="data_integrity",
            condition="data_integrity_degraded",
            cooldown_seconds=900.0,
            whatsapp=True,
            webhook=False,
            log=True,
            description="Runtime Redis/arquivo divergiram ou mirror degradado ativo",
        ),
        AlertRuleConfig(
            name="redis-write-failures",
            source="data_integrity",
            condition="redis_write_failures >= 1",
            cooldown_seconds=600.0,
            whatsapp=True,
            webhook=False,
            log=True,
            description="Falhas ao gravar estado de runtime no Redis",
        ),
        AlertRuleConfig(
            name="ruflo-swarm-not-ok",
            source="ruflo_health",
            condition="consecutive_failures >= 3",
            cooldown_seconds=900.0,
            whatsapp=True,
            webhook=False,
            log=True,
            description="RuFlo /ruflo/status falhou 3× seguidas (swarm_ok ou latência)",
        ),
        AlertRuleConfig(
            name="ruflo-status-slow",
            source="ruflo_health",
            condition="ruflo_latency_ms >= 12000",
            cooldown_seconds=900.0,
            whatsapp=True,
            webhook=False,
            log=True,
            description="RuFlo status demorou ≥12s",
        ),
    ]
