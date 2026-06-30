"""Hermes cron diagnostic profile + LLM agent (read-only; recovery is separate)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .config import HermesCronRecoveryConfig
from . import hermes_profiles as _hermes_profiles
from .hermes_profiles import TEAM_ROLE_PRESETS, preset_agent_spec, seed_team_role_presets_filesystem

log = logging.getLogger(__name__)

HERMES_CRON_DIAGNOSTIC_PRESET_ID = "hermes-cron-diagnostic"

# Read-only tools for the diagnostic LLM loop (no restart / retry).
CRON_DIAGNOSTIC_TOOL_NAMES = frozenset(
    {
        "health_check",
        "process_status",
        "read_log_tail",
        "hermes_cron_scheduler_status",
        "hermes_cron_snapshot",
        "hermes_cron_diagnostic_report",
        "hermes_cron_job_result",
    }
)


def _hermes_cron_diagnostic_preset() -> dict[str, str]:
    for preset in TEAM_ROLE_PRESETS:
        if str(preset.get("id") or "") == HERMES_CRON_DIAGNOSTIC_PRESET_ID:
            return preset
    return {
        "id": HERMES_CRON_DIAGNOSTIC_PRESET_ID,
        "label": "Agente de diagnóstico cron Hermes",
        "category": "operacoes-ti",
        "prompt": (
            "analista de falhas em crons Hermes: scheduler, jobs.json, logs e outputs "
            "— diagnóstico sem reiniciar serviços"
        ),
    }


def hermes_cron_diagnostic_agent_spec():
    return preset_agent_spec(_hermes_cron_diagnostic_preset())


def ensure_hermes_cron_diagnostic_profile(
    *,
    profile_id: Optional[str] = None,
    template_profile: Optional[str] = None,
) -> dict[str, Any]:
    pid = (profile_id or HERMES_CRON_DIAGNOSTIC_PRESET_ID).strip()
    result = seed_team_role_presets_filesystem(
        only_missing=True,
        preset_ids=[pid],
        template_profile=template_profile,
        profiles_root=_hermes_profiles.hermes_profiles_root(),
        progress_every=0,
    )
    if result.get("created"):
        log.info("hermes cron diagnostic profile created on disk: %s", pid)
    return result


def default_hermes_cron_diagnostic_system_prompt() -> str:
    return (
        "You are the Hermes cron **diagnostic** agent used by gois.\n"
        "Your job is to explain why cron jobs fail or do not run — **do not** restart "
        "gateway, dashboard, or re-run jobs unless the operator explicitly asks elsewhere.\n"
        "Workflow:\n"
        "1. hermes_cron_diagnostic_report — structured issues from the monitor.\n"
        "2. hermes_cron_scheduler_status — gateway vs jobs_path HERMES_HOME.\n"
        "3. hermes_cron_snapshot — jobs with last_status=error.\n"
        "4. read_log_tail on errors.log / gateway.log for cron.scheduler lines.\n"
        "5. hermes_cron_job_result(job_id) when investigating one job.\n"
        "End with: causa provável, evidências, passos recomendados (em português). "
        "Mencione se o problema é scheduler parado, HERMES_HOME errado, ou falha do job."
    )


@dataclass(frozen=True)
class HermesCronDiagnosticExtras:
    """Tools for cron diagnosis (read-only)."""

    cron_cfg: HermesCronRecoveryConfig
    diagnostic_report: Callable[[Optional[str]], dict[str, Any]]
    cron_snapshot: Callable[[], dict[str, Any]]
    cron_job_result: Callable[[str], dict[str, Any]]


def build_hermes_cron_diagnostic_extras(
    monitor: Any,
    *,
    require_agent_enabled: bool = True,
) -> Optional[HermesCronDiagnosticExtras]:
    if not monitor.cfg.hermes:
        return None
    cda = monitor.cfg.hermes_cron_diagnostic_agent
    if require_agent_enabled and (not cda.enabled or not cda.llm_enabled):
        return None

    def _report(job_id: Optional[str] = None) -> dict[str, Any]:
        return monitor.build_hermes_cron_diagnostic_report(job_id=job_id)

    def _snap() -> dict[str, Any]:
        return monitor._cached_hermes_cron_snapshot()

    def _job_result(job_id: str) -> dict[str, Any]:
        return monitor.handle_hermes_cron_result(job_id)

    return HermesCronDiagnosticExtras(
        cron_cfg=monitor.cfg.hermes_cron_recovery,
        diagnostic_report=_report,
        cron_snapshot=_snap,
        cron_job_result=_job_result,
    )
