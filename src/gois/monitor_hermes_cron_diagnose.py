"""Hermes cron diagnostic reports and advanced job preview."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .hermes_cron import (
    compose_cron_schedule_from_builder,
    compute_next_run_at_for_job,
    normalize_cron_schedule_value,
)
from .hermes_cron_diagnostic import (
    build_hermes_cron_diagnostic_report,
    hermes_cron_diagnostic_headline,
)
from .hermes_cron_diagnostic_agent import ensure_hermes_cron_diagnostic_profile

log = logging.getLogger(__name__)


class MonitorHermesCronDiagnoseMixin:
    def build_hermes_cron_diagnostic_report(
        self, *, job_id: Optional[str] = None
    ) -> dict[str, Any]:
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        jobs_path = self._hermes_cron_jobs_path()
        log_paths = list(self.cfg.hermes.log_paths or [])
        return build_hermes_cron_diagnostic_report(
            jobs_path=jobs_path,
            hermes_log_paths=log_paths,
            job_id=job_id,
            gateway_up=self._hermes_is_up(),
        )

    async def _refresh_hermes_cron_diagnostic_report(
        self, *, job_id: Optional[str] = None
    ) -> dict[str, Any]:
        report = await asyncio.to_thread(
            lambda: self.build_hermes_cron_diagnostic_report(job_id=job_id)
        )
        self._hermes_cron_diagnostic_cache = dict(report)
        return report

    def _hermes_cron_diagnostic_status_payload(self) -> dict[str, Any]:
        cache = self._hermes_cron_diagnostic_cache
        return {
            "headline": hermes_cron_diagnostic_headline(cache) if cache else None,
            "healthy": cache.get("healthy") if cache else None,
            "generated_at": cache.get("generated_at") if cache else None,
            "issues": (cache.get("issues") or [])[:12] if cache else [],
            "recommendations": (cache.get("recommendations") or [])[:8] if cache else [],
            "llm_report": (
                (self.state.last_hermes_cron_diagnostic_report or "")[:2000] or None
            ),
            "llm_ts": self.state.last_hermes_cron_diagnostic_ts,
            "scheduler_agent_report": (
                (self.state.last_hermes_cron_scheduler_recovery_report or "")[:2000]
                or None
            ),
            "scheduler_agent_ts": self.state.last_hermes_cron_scheduler_recovery_ts,
        }

    def handle_hermes_cron_diagnose(
        self, query: Optional[dict] = None
    ) -> dict[str, Any]:
        """Structured cron diagnostic report (optional ?job_id=)."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        q = query or {}
        job_id = str(q.get("job_id") or "").strip() or None
        report = self.build_hermes_cron_diagnostic_report(job_id=job_id)
        self._hermes_cron_diagnostic_cache = dict(report)
        cda = self.cfg.hermes_cron_diagnostic_agent
        return {
            "ok": True,
            "report": report,
            "agent": {
                "enabled": cda.enabled,
                "llm_enabled": cda.llm_enabled,
                "profile_id": cda.profile_id,
            },
            "last_llm_report": (
                (self.state.last_hermes_cron_diagnostic_report or "")[:2000]
                if self.state.last_hermes_cron_diagnostic_report
                else None
            ),
            "last_ts": self.state.last_hermes_cron_diagnostic_ts,
        }

    def handle_hermes_cron_diagnose_action(self, payload: dict) -> dict[str, Any]:
        return asyncio.run(self._hermes_cron_diagnose_action_async(payload))

    async def _hermes_cron_diagnose_action_async(self, payload: dict) -> dict[str, Any]:
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        action = str(payload.get("action") or "diagnose").strip().lower()
        job_id = str(payload.get("job_id") or "").strip() or None
        if action == "ensure_profile":
            cda = self.cfg.hermes_cron_diagnostic_agent
            result = await asyncio.to_thread(
                ensure_hermes_cron_diagnostic_profile,
                profile_id=cda.profile_id,
                template_profile=self.cfg.hermes_agent_create.seed_role_catalog_template_profile,
            )
            return {"ok": bool(result.get("ok", True)), **result}
        report = self.build_hermes_cron_diagnostic_report(job_id=job_id)
        if action in {"diagnose", "report"}:
            return {"ok": True, "action": action, "report": report}
        if action == "run_agent":
            text = await self._run_hermes_cron_diagnostic_agent(
                report=report,
                reason="POST /hermes/cron/diagnose",
                job_id=job_id,
            )
            return {
                "ok": bool(self.state.last_hermes_cron_diagnostic_ok),
                "action": action,
                "report": report,
                "llm_report": text,
            }
        return {"ok": False, "error": f"unknown action {action!r}"}

    # ---- advanced cron job creation (UI: /jobs/novo) ------------------------

    def _resolve_advanced_cron_payload(self, payload: dict) -> dict:
        """Normalize advanced-job builder payload into Hermes-CLI inputs."""
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")

        prompt_raw = payload.get("prompt")
        if not isinstance(prompt_raw, str) or not prompt_raw.strip():
            raise ValueError("prompt is required")

        # Schedule: accept either builder fields or raw 'schedule'
        schedule_fields = (
            "builder_kind",
            "sched_kind",
            "interval_minutes",
            "interval_value",
            "interval_unit",
            "cron_preset",
            "cron_expr",
            "run_at",
        )
        if any(k in payload for k in schedule_fields):
            schedule = compose_cron_schedule_from_builder(payload)
        else:
            raw_sched = payload.get("schedule")
            if not isinstance(raw_sched, str) or not raw_sched.strip():
                raise ValueError("schedule is required")
            schedule = raw_sched.strip()

        name = payload.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError("name must be a string")
        profile = payload.get("profile")
        if profile is not None and not isinstance(profile, str):
            raise ValueError("profile must be a string")
        workdir = payload.get("workdir")
        if workdir is not None and not isinstance(workdir, str):
            raise ValueError("workdir must be a string")
        skills_in = payload.get("skills") or []
        if not isinstance(skills_in, list):
            raise ValueError("skills must be a list")
        skills = [str(s).strip() for s in skills_in if str(s or "").strip()]

        repeat = payload.get("repeat")
        if repeat is not None:
            try:
                repeat = int(repeat)
            except (TypeError, ValueError):
                raise ValueError("repeat must be an integer")

        # Optional informational sections appended to the prompt.
        prompt = prompt_raw.strip()
        env_pairs = payload.get("env") or []
        if isinstance(env_pairs, dict):
            env_pairs = [{"key": k, "value": v} for k, v in env_pairs.items()]
        env_lines: list[str] = []
        if isinstance(env_pairs, list):
            for item in env_pairs:
                if isinstance(item, dict):
                    k = str(item.get("key") or "").strip()
                    v = item.get("value")
                    if not k:
                        continue
                    secret = bool(item.get("secret"))
                    shown = "***" if secret else str(v if v is not None else "")
                    env_lines.append(f"- {k}={shown}")
        if env_lines:
            prompt += "\n\n## Variáveis de ambiente esperadas\n" + "\n".join(env_lines)

        notifications = payload.get("notifications") or {}
        notif_lines: list[str] = []
        if isinstance(notifications, dict):
            for channel in ("whatsapp", "email", "webhook"):
                target = notifications.get(channel)
                if isinstance(target, str) and target.strip():
                    notif_lines.append(f"- {channel}: {target.strip()}")
            if notifications.get("on_failure_only"):
                notif_lines.append("- gatilho: somente em falha")
        if notif_lines:
            prompt += "\n\n## Notificações ao concluir\n" + "\n".join(notif_lines)

        return {
            "schedule": schedule,
            "prompt": prompt,
            "name": (name.strip() if isinstance(name, str) and name.strip() else None),
            "profile": (profile.strip() if isinstance(profile, str) and profile.strip() else None),
            "skills": skills,
            "workdir": (workdir.strip() if isinstance(workdir, str) and workdir.strip() else None),
            "repeat": repeat,
        }

    def handle_hermes_cron_preview(self, payload: dict) -> dict:
        """Dry-run: validate builder + return resolved schedule, argv, next_run preview."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        try:
            resolved = self._resolve_advanced_cron_payload(payload or {})
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        schedule_raw = resolved["schedule"]
        staggered = self._stagger_schedule_for_new_job(schedule_raw)
        norm, _ = normalize_cron_schedule_value(staggered, display_hint=staggered)

        from .hermes_cron import build_cron_create_argv

        argv = build_cron_create_argv(
            staggered,
            resolved["prompt"],
            name=resolved["name"],
            profile=resolved["profile"],
            skills=resolved["skills"],
            workdir=resolved["workdir"],
            repeat=resolved["repeat"],
            accept_hooks=self.cfg.hermes_agent_create.cron_accept_hooks
            if self.cfg.hermes_agent_create
            else False,
        )

        fake_job = {"schedule": norm if isinstance(norm, dict) else {"kind": "raw", "raw": staggered}}
        next_run = compute_next_run_at_for_job(fake_job, anchor_from_last_run=False)

        return {
            "ok": True,
            "schedule": schedule_raw,
            "staggered_schedule": staggered,
            "schedule_normalized": norm,
            "argv": argv,
            "next_run_at": next_run,
            "prompt_preview": resolved["prompt"],
            "warnings": (
                ["Sem `croniter` instalado: pré-visualização de cron expressions limitada."]
                if next_run is None and isinstance(norm, dict) and norm.get("kind") == "cron"
                else []
            ),
        }

    # ------------------------------------------------------------------
    # Knowledge base extracted from chat history
    # ------------------------------------------------------------------

