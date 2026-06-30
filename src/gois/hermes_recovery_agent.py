"""Hermes recovery profile + LLM recovery extras for gois."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .config import HermesCronRecoveryConfig, HermesRecoveryAgentConfig
from . import hermes_profiles as _hermes_profiles
from .hermes_profiles import TEAM_ROLE_PRESETS, preset_agent_spec, seed_team_role_presets_filesystem

log = logging.getLogger(__name__)

HERMES_RECOVERY_PRESET_ID = "hermes-recovery"


def _hermes_recovery_preset() -> dict[str, str]:
    for preset in TEAM_ROLE_PRESETS:
        if str(preset.get("id") or "") == HERMES_RECOVERY_PRESET_ID:
            return preset
    return {
        "id": HERMES_RECOVERY_PRESET_ID,
        "label": "Agente de recuperação Hermes",
        "category": "operacoes-ti",
        "prompt": "operador SRE do Hermes",
    }


def hermes_recovery_agent_spec():
    """SOUL.md content for the dedicated Hermes recovery profile."""
    return preset_agent_spec(_hermes_recovery_preset())


def ensure_hermes_recovery_profile(
    *,
    profile_id: Optional[str] = None,
    template_profile: Optional[str] = None,
) -> dict[str, Any]:
    """Create the hermes-recovery profile on disk if missing."""
    pid = (profile_id or HERMES_RECOVERY_PRESET_ID).strip()
    result = seed_team_role_presets_filesystem(
        only_missing=True,
        preset_ids=[pid],
        template_profile=template_profile,
        profiles_root=_hermes_profiles.hermes_profiles_root(),
        progress_every=0,
    )
    if result.get("created"):
        log.info("hermes recovery profile created on disk: %s", pid)
    return result


@dataclass(frozen=True)
class HermesRecoveryExtras:
    """Extra tools passed to run_recovery_agent when recovering Hermes."""

    cron_cfg: HermesCronRecoveryConfig
    dashboard_start_command: list[str]
    cron_snapshot: Optional[Callable[[], dict[str, Any]]] = None


def build_hermes_recovery_extras(
    monitor: Any,
) -> Optional[HermesRecoveryExtras]:
    """Build extras from a GoisMonitor instance (typed as Any to avoid cycles)."""
    if not monitor.cfg.hermes or not monitor.hermes_recovery:
        return None
    hra = monitor.cfg.hermes_recovery_agent
    if not hra.enabled or not hra.llm_enabled:
        return None
    snapshot_fn: Optional[Callable[[], dict[str, Any]]] = None
    if monitor.cfg.hermes_cron_recovery.enabled:

        def _snap() -> dict[str, Any]:
            return monitor._cached_hermes_cron_snapshot()

        snapshot_fn = _snap
    return HermesRecoveryExtras(
        cron_cfg=monitor.cfg.hermes_cron_recovery,
        dashboard_start_command=monitor._hermes_dashboard_start_command(),
        cron_snapshot=snapshot_fn,
    )


def default_hermes_recovery_system_prompt() -> str:
    return (
        "You are the Hermes SRE recovery agent used by gois.\n"
        "Hermes includes: (1) gateway process `hermes gateway run`, "
        "(2) web dashboard (default http://127.0.0.1:9119), "
        "(3) scheduled cron jobs under hermes/cron/jobs.json, "
        "(4) per-agent profiles on disk.\n"
        "Use tools conservatively: gather health_check, process_status, and log "
        "tails before restarting. Prefer restart_hermes_dashboard when only the "
        "UI is down; use restart_hermes for gateway crashes. Cron jobs only run when "
        "a gateway is up for the same HERMES_HOME as jobs_path — call "
        "hermes_cron_scheduler_status first; if blocked, call ensure_hermes_cron_gateway "
        "before hermes_cron_retry. Use hermes_cron_snapshot to list failing jobs.\n"
        "Reply with a short final report in Portuguese when done."
    )
