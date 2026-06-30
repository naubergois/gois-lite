"""Dedicated LLM agent for Hermes cron scheduler / gateway recovery."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from .config import HermesCronRecoveryConfig
from . import hermes_profiles as _hermes_profiles
from .hermes_profiles import TEAM_ROLE_PRESETS, preset_agent_spec, seed_team_role_presets_filesystem
from .hermes_recovery_agent import HermesRecoveryExtras

log = logging.getLogger(__name__)

HERMES_CRON_RECOVERY_PRESET_ID = "hermes-cron-recovery"

# Tools exposed to the cron-scheduler recovery agent (subset of full Hermes SRE).
CRON_SCHEDULER_RECOVERY_TOOL_NAMES = frozenset(
    {
        "health_check",
        "process_status",
        "read_log_tail",
        "restart_hermes",
        "hermes_cron_scheduler_status",
        "ensure_hermes_cron_gateway",
        "hermes_cron_snapshot",
        "hermes_cron_retry",
        "hermes_cron_diagnostic_report",
        "hermes_cron_job_result",
    }
)


def _hermes_cron_recovery_preset() -> dict[str, str]:
    for preset in TEAM_ROLE_PRESETS:
        if str(preset.get("id") or "") == HERMES_CRON_RECOVERY_PRESET_ID:
            return preset
    return {
        "id": HERMES_CRON_RECOVERY_PRESET_ID,
        "label": "Agente de recuperação cron Hermes",
        "category": "operacoes-ti",
        "prompt": (
            "especialista em crons Hermes: gateway do scheduler, jobs.json, "
            "falhas de execução e re-disparo de jobs"
        ),
    }


def hermes_cron_recovery_agent_spec():
    """SOUL.md for the cron-focused Hermes recovery profile."""
    return preset_agent_spec(_hermes_cron_recovery_preset())


def ensure_hermes_cron_recovery_profile(
    *,
    profile_id: Optional[str] = None,
    template_profile: Optional[str] = None,
) -> dict[str, Any]:
    """Create the hermes-cron-recovery profile on disk if missing."""
    pid = (profile_id or HERMES_CRON_RECOVERY_PRESET_ID).strip()
    result = seed_team_role_presets_filesystem(
        only_missing=True,
        preset_ids=[pid],
        template_profile=template_profile,
        profiles_root=_hermes_profiles.hermes_profiles_root(),
        progress_every=0,
    )
    if result.get("created"):
        log.info("hermes cron recovery profile created on disk: %s", pid)
    return result


def build_hermes_cron_scheduler_recovery_extras(
    monitor: Any,
) -> Optional[HermesRecoveryExtras]:
    """Build Hermes extras for the cron-scheduler-only recovery agent."""
    if not monitor.cfg.hermes or not monitor.hermes_recovery:
        return None
    csa = monitor.cfg.hermes_cron_scheduler_agent
    if not csa.enabled or not csa.llm_enabled:
        return None
    if not monitor.cfg.hermes_cron_recovery.enabled:
        return None

    def _snap() -> dict[str, Any]:
        return monitor._cached_hermes_cron_snapshot()

    return HermesRecoveryExtras(
        cron_cfg=monitor.cfg.hermes_cron_recovery,
        dashboard_start_command=monitor._hermes_dashboard_start_command(),
        cron_snapshot=_snap,
    )


def default_hermes_cron_scheduler_recovery_system_prompt() -> str:
    return (
        "You are the Hermes cron scheduler recovery agent used by gois.\n"
        "Your mission: explain why crons are not firing, then restore the scheduler.\n"
        "Cron jobs run only when a Hermes gateway is up for the same HERMES_HOME as "
        "cron/jobs.json.\n"
        "Workflow:\n"
        "1. Read structured_diagnostic in the failure summary and call "
        "hermes_cron_diagnostic_report to confirm root cause.\n"
        "2. hermes_cron_scheduler_status — confirm gateway down and note hermes_home.\n"
        "3. ensure_hermes_cron_gateway — start gateway for that HERMES_HOME.\n"
        "4. hermes_cron_scheduler_status again — verify ok.\n"
        "5. hermes_cron_snapshot — list jobs with last_status=error.\n"
        "6. hermes_cron_retry for failed jobs if logs mention scheduler failures.\n"
        "Use restart_hermes only if ensure_hermes_cron_gateway cannot fix the gateway.\n"
        "Final report in Portuguese with sections:\n"
        "## Diagnóstico\n(causa raiz e evidências)\n"
        "## Ações\n(o que fez)\n"
        "## Resultado\n(scheduler ok ou pendente)"
    )
