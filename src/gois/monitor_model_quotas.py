"""Model daily quota enforcement and HTTP handlers for GoisMonitor."""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .accounts import UserRecord
from .hermes_cron import (
    CronJobsPauseSnapshot,
    maintain_recurring_cron_schedule,
    pause_all_active_hermes_cron_jobs,
    resume_hermes_cron_jobs_from_snapshot,
)

log = logging.getLogger(__name__)

class MonitorModelQuotasMixin:
    def _model_daily_quota_status(self, *, enforce: bool = False) -> dict[str, Any]:
        if enforce:
            self._enforce_model_daily_quotas()
        day = self._today_key()
        limits = self._coerce_model_daily_quotas()
        usd_limits = self._coerce_model_daily_usd_quotas()
        usd_prices = self._coerce_model_usd_per_1k_prices()
        usage = self._cached_model_daily_usage(day_iso=day)
        models_usage = usage.get("models") if isinstance(usage.get("models"), dict) else {}
        rows: list[dict[str, Any]] = []
        exceeded_models: list[str] = []
        # Include models from cron token stats so that models appearing in jobs
        # are always present in usage.models even when they have no daily usage yet.
        cron_stats_models: set[str] = set()
        try:
            agent_log_path = resolve_agent_log_path(
                self.cfg.hermes.log_paths if self.cfg.hermes else None
            )
            cron_stats = self._cached_hermes_cron_token_stats(agent_log_path)
            for stats in cron_stats.values():
                m = stats.get("model") if isinstance(stats, dict) else None
                if m and isinstance(m, str):
                    cron_stats_models.add(m.strip())
        except Exception:
            pass
        all_models = sorted(
            set(limits)
            | set(usd_limits)
            | set(usd_prices)
            | set(models_usage)
            | cron_stats_models
        )
        for model in all_models:
            urow = models_usage.get(model)
            if not isinstance(urow, dict):
                urow = {}
            used = int(urow.get("total_tokens") or 0)
            runs = int(urow.get("runs") or 0)
            limit = limits.get(model)
            usd_per_1k = usd_prices.get(model)
            usd_used = ((used / 1000.0) * usd_per_1k) if usd_per_1k else None
            usd_limit = usd_limits.get(model)
            usd_exceeded = bool(usd_limit and usd_used is not None and usd_used > usd_limit)
            exceeded = bool((limit and used > limit) or usd_exceeded)
            if exceeded:
                exceeded_models.append(model)
            rows.append(
                {
                    "model": model,
                    "tokens_used": used,
                    "runs": runs,
                    "tokens_limit": limit,
                    "remaining_tokens": (max(0, limit - used) if limit else None),
                    "usd_per_1k": usd_per_1k,
                    "usd_used": (round(usd_used, 6) if usd_used is not None else None),
                    "usd_limit": usd_limit,
                    "remaining_usd": (
                        round(max(0.0, usd_limit - usd_used), 6)
                        if (usd_limit and usd_used is not None)
                        else None
                    ),
                    "exceeded": exceeded,
                    "max_tokens": int(urow.get("max_tokens") or 0),
                    "avg_tokens": int(urow.get("avg_tokens") or 0),
                }
            )

        block = self.state.model_daily_quota_block if isinstance(
            self.state.model_daily_quota_block, dict
        ) else None
        block_active = bool(block and str(block.get("date") or "") == day)
        return {
            "ok": True,
            "enabled": bool(limits or usd_limits),
            "day": day,
            "quotas": [
                {
                    "model": m,
                    "tokens_per_day": limits.get(m),
                    "usd_per_day": usd_limits.get(m),
                    "usd_per_1k": usd_prices.get(m),
                }
                for m in sorted(set(limits.keys()) | set(usd_limits.keys()) | set(usd_prices.keys()))
            ],
            "prices": dict(sorted(usd_prices.items())),
            "usage": {
                "total_tokens": int(usage.get("total_tokens") or 0),
                "total_runs": int(usage.get("total_runs") or 0),
                "models": rows,
            },
            "blocked": block_active,
            "block": block if block_active else None,
            "exceeded_models": exceeded_models,
        }

    def _resume_model_quota_block_if_new_day(self, *, today: Optional[str] = None) -> None:
        block = self.state.model_daily_quota_block
        if not isinstance(block, dict):
            return
        current_day = today or self._today_key()
        block_day = str(block.get("date") or "")
        if not block_day or block_day == current_day:
            return
        paused_ids_raw = block.get("paused_job_ids") or []
        paused_ids = [str(x).strip() for x in paused_ids_raw if str(x).strip()]
        resumed: dict[str, Any] = {"ok": True, "resumed_count": 0, "failures": []}
        if self.cfg.hermes and paused_ids:
            try:
                jobs_path = self._hermes_cron_jobs_path()
                resumed = resume_hermes_cron_jobs_from_snapshot(
                    CronJobsPauseSnapshot(paused_job_ids=tuple(paused_ids)),
                    jobs_path,
                    accept_hooks=self.cfg.hermes_cron_recovery.accept_hooks,
                    timeout_seconds=self.cfg.hermes_cron_recovery.timeout_seconds,
                )
                cc = self.cfg.hermes_cron_recovery
                repair = maintain_recurring_cron_schedule(
                    jobs_path,
                    stale_hours=float(cc.stale_cron_hours or 48.0),
                )
                resumed["cron_repair"] = {
                    "repaired": repair.get("repaired"),
                    "stale_after": repair.get("stale_after"),
                    "summary": repair.get("summary"),
                }
                self._invalidate_hermes_cron_cache()
            except Exception as exc:
                resumed = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self.state.model_daily_quota_block = None
        self._persist()
        log.info(
            "model quota block reset for new day (previous=%s, resumed=%s)",
            block_day,
            resumed,
        )

    def _enforce_model_daily_quotas(self) -> dict[str, Any]:
        if not self.cfg.hermes:
            return {"ok": True, "enabled": False}
        today = self._today_key()
        self._resume_model_quota_block_if_new_day(today=today)
        limits = self._coerce_model_daily_quotas()
        usd_limits = self._coerce_model_daily_usd_quotas()
        usd_prices = self._coerce_model_usd_per_1k_prices()
        if not limits and not usd_limits:
            return {"ok": True, "enabled": False}

        usage = self._cached_model_daily_usage(day_iso=today)
        models = usage.get("models") if isinstance(usage.get("models"), dict) else {}
        exceeded: list[dict[str, Any]] = []
        for model, limit in limits.items():
            used = 0
            row = models.get(model)
            if isinstance(row, dict):
                used = int(row.get("total_tokens") or 0)
            if used > int(limit):
                exceeded.append(
                    {
                        "model": model,
                        "metric": "tokens",
                        "used": used,
                        "limit": int(limit),
                        "over": used - int(limit),
                    }
                )
        for model, usd_limit in usd_limits.items():
            used_tokens = 0
            row = models.get(model)
            if isinstance(row, dict):
                used_tokens = int(row.get("total_tokens") or 0)
            usd_per_1k = usd_prices.get(model)
            if not usd_per_1k:
                continue
            used_usd = (used_tokens / 1000.0) * usd_per_1k
            if used_usd > float(usd_limit):
                exceeded.append(
                    {
                        "model": model,
                        "metric": "usd",
                        "used": round(used_usd, 6),
                        "limit": round(float(usd_limit), 6),
                        "over": round(used_usd - float(usd_limit), 6),
                    }
                )
        if not exceeded:
            return {"ok": True, "enabled": True, "blocked": False}

        block = self.state.model_daily_quota_block
        if isinstance(block, dict) and str(block.get("date") or "") == today:
            block["exceeded_models"] = exceeded
            block["last_checked_at"] = time.time()
            self.state.model_daily_quota_block = block
            self._persist()
            return {"ok": True, "enabled": True, "blocked": True, "already_blocked": True}

        jobs_path = self._hermes_cron_jobs_path()
        snap, pause_result = pause_all_active_hermes_cron_jobs(
            jobs_path,
            accept_hooks=self.cfg.hermes_cron_recovery.accept_hooks,
            timeout_seconds=self.cfg.hermes_cron_recovery.timeout_seconds,
        )
        self._invalidate_hermes_cron_cache()
        self.state.model_daily_quota_block = {
            "date": today,
            "triggered_at": time.time(),
            "paused_job_ids": list(snap.paused_job_ids),
            "paused_count": len(snap.paused_job_ids),
            "pause_result": pause_result,
            "exceeded_models": exceeded,
        }
        self._persist()
        log.warning(
            "model daily quota exceeded (%s); paused cron jobs=%d",
            ", ".join(
                f"{row['model']}[{row.get('metric') or 'tokens'}] {row['used']}/{row['limit']}"
                for row in exceeded
            ),
            len(snap.paused_job_ids),
        )
        return {
            "ok": True,
            "enabled": True,
            "blocked": True,
            "paused_count": len(snap.paused_job_ids),
            "exceeded_models": exceeded,
        }

    def _model_quota_guard(self) -> Optional[dict[str, Any]]:
        status = self._model_daily_quota_status(enforce=True)
        if not status.get("blocked"):
            return None
        block = status.get("block") if isinstance(status.get("block"), dict) else {}
        return {
            "ok": False,
            "error": "agendamentos suspensos hoje por cota diária de modelo excedida",
            "quota": {
                "day": status.get("day"),
                "exceeded_models": status.get("exceeded_models") or [],
                "paused_count": block.get("paused_count"),
            },
        }

    def handle_model_quotas_get(self, user: Optional[UserRecord] = None) -> dict[str, Any]:
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        return self._model_daily_quota_status(enforce=True)

    def handle_model_quotas_update(
        self,
        payload: dict,
        user: Optional[UserRecord] = None,
    ) -> dict[str, Any]:
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if self.cfg.auth.enabled and actor is not None and not actor.is_admin:
            return {"ok": False, "error": "admin required"}

        body = payload if isinstance(payload, dict) else {}
        quotas = body.get("quotas")
        has_quotas_rows = isinstance(quotas, list)
        next_limits: dict[str, int] = {}
        next_usd_limits: dict[str, float] = (
            {} if ("usd_limits" in body or has_quotas_rows) else self._coerce_model_daily_usd_quotas()
        )
        next_prices: dict[str, float] = (
            {} if ("prices" in body or has_quotas_rows) else self._coerce_model_usd_per_1k_prices()
        )
        raw = body.get("limits")
        if isinstance(raw, dict):
            for model_raw, limit_raw in raw.items():
                model = str(model_raw or "").strip()
                if not model:
                    continue
                try:
                    limit = int(limit_raw)
                except Exception:
                    continue
                if limit > 0:
                    next_limits[model] = limit
        raw_usd_limits = body.get("usd_limits")
        if isinstance(raw_usd_limits, dict):
            for model_raw, limit_raw in raw_usd_limits.items():
                model = str(model_raw or "").strip()
                if not model:
                    continue
                try:
                    limit = float(limit_raw)
                except Exception:
                    continue
                if limit > 0:
                    next_usd_limits[model] = round(limit, 6)
        raw_prices = body.get("prices")
        if isinstance(raw_prices, dict):
            for model_raw, price_raw in raw_prices.items():
                model = str(model_raw or "").strip()
                if not model:
                    continue
                try:
                    price = float(price_raw)
                except Exception:
                    continue
                if price > 0:
                    next_prices[model] = round(price, 6)
        if isinstance(quotas, list):
            for row in quotas:
                if not isinstance(row, dict):
                    continue
                model = str(row.get("model") or "").strip()
                if not model:
                    continue
                try:
                    limit = int(row.get("tokens_per_day"))
                except Exception:
                    limit = None
                if limit is not None and limit > 0:
                    next_limits[model] = int(limit)
                usd_limit_raw = row.get("usd_per_day")
                try:
                    usd_limit = float(usd_limit_raw)
                except Exception:
                    usd_limit = None
                if usd_limit is not None and usd_limit > 0:
                    next_usd_limits[model] = round(usd_limit, 6)
                usd_price_raw = row.get("usd_per_1k")
                try:
                    usd_price = float(usd_price_raw)
                except Exception:
                    usd_price = None
                if usd_price is not None and usd_price > 0:
                    next_prices[model] = round(usd_price, 6)

        self.state.model_daily_token_quotas = dict(sorted(next_limits.items()))
        self.state.model_daily_usd_quotas = dict(sorted(next_usd_limits.items()))
        self.state.model_usd_per_1k_prices = dict(sorted(next_prices.items()))

        if str(body.get("resume_now") or "").strip().lower() in ("1", "true", "yes", "on"):
            self._resume_model_quota_block_if_new_day(today="__force_resume__")
            self.state.model_daily_quota_block = None

        self._persist()
        self._enforce_model_daily_quotas()
        return self._model_daily_quota_status(enforce=False)
