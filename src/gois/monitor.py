"""Async health-check loop with failure-threshold + cooldown gating
around the LLM recovery agent. Persists state and exposes metrics.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from .alerting import AlertEngine, default_alert_rules
from .chat_history import ChatPersistence
from .chat_jobs import init_chat_jobs_persistence
from .config import Config
from .knowledge_base import (
    KnowledgeStore,
    default_store_path as knowledge_default_store_path,
)
from .log_scanner import LogPattern, LogScanner
from .metrics import Metrics
from .monitor_active_agents import MonitorActiveAgentsMixin
from .monitor_auth import MonitorAuthMixin
from .monitor_background_loops import MonitorBackgroundLoopsMixin
from .monitor_cron_concurrency import MonitorCronConcurrencyMixin
from .monitor_env_keys import MonitorEnvKeysMixin
from .monitor_health_loops import MonitorHealthLoopsMixin
from .monitor_hermes_agent_create import MonitorHermesAgentCreateMixin
from .monitor_hermes_catalog import MonitorHermesCatalogMixin
from .monitor_hermes_cron import MonitorHermesCronMixin
from .monitor_hermes_cron_actions import MonitorHermesCronActionsMixin
from .monitor_hermes_cron_diagnose import MonitorHermesCronDiagnoseMixin
from .monitor_hermes_cron_force_remove import MonitorHermesCronForceRemoveMixin
from .monitor_hermes_cron_handlers import MonitorHermesCronHandlersMixin
from .monitor_hermes_cron_paths import MonitorHermesCronPathsMixin
from .monitor_hermes_cron_results import MonitorHermesCronResultsMixin
from .monitor_hermes_dashboard_helpers import MonitorHermesDashboardHelpersMixin
from .monitor_hermes_kanban import MonitorHermesKanbanMixin
from .monitor_hermes_kanban_board import MonitorHermesKanbanBoardMixin
from .monitor_hermes_kanban_handlers import MonitorHermesKanbanHandlersMixin
from .monitor_hermes_kanban_schedule import MonitorHermesKanbanScheduleMixin
from .monitor_hermes_kanban_schedule_cron import MonitorHermesKanbanScheduleCronMixin
from .monitor_hermes_kanban_schedule_exec import MonitorHermesKanbanScheduleExecMixin
from .monitor_hermes_profiles import MonitorHermesProfilesMixin
from .monitor_hermes_recovery import MonitorHermesRecoveryMixin
from .monitor_kanban_cron_boards import MonitorKanbanCronBoardsMixin
from .monitor_kanban_cron_delegate import MonitorKanbanCronDelegateMixin
from .monitor_kanban_cron_execution import MonitorKanbanCronExecutionMixin
from .monitor_kanban_cron_scheduling import MonitorKanbanCronSchedulingMixin
from .monitor_kanban_cron_sync import MonitorKanbanCronSyncMixin
from .monitor_knowledge import MonitorKnowledgeMixin
from .monitor_attendance import MonitorAttendanceMixin
from .monitor_latex import MonitorLatexMixin
from .monitor_leader import MonitorLeaderMixin
from .monitor_model_quotas import MonitorModelQuotasMixin
from .monitor_swarm_cron_policy import MonitorSwarmCronPolicyMixin
from .monitor_model_usage import MonitorModelUsageMixin
from .monitor_mongodb_keepalive import MonitorMongodbKeepaliveMixin
from .monitor_openclaw_chat_status import MonitorOpenclawChatStatusMixin
from .monitor_openclaw_send_handlers import MonitorOpenclawSendHandlersMixin
from .monitor_openclaw_send_jobs import MonitorOpenclawSendJobsMixin
from .monitor_openclaw_sessions import MonitorOpenclawSessionsMixin
from .monitor_openclaw_skills import MonitorOpenclawSkillsMixin
from .monitor_persist import MonitorPersistMixin
from .monitor_priority_queue import MonitorPriorityQueueMixin
from .monitor_recovery_hermes_cron import MonitorRecoveryHermesCronMixin
from .monitor_recovery_misc import MonitorRecoveryMiscMixin
from .monitor_recovery_triggers import MonitorRecoveryTriggersMixin
from .monitor_ruflo import MonitorRufloMixin
from .monitor_run_bootstrap import MonitorRunBootstrapMixin
from .monitor_startup import MonitorStartupMixin
from .monitor_status import MonitorStatusMixin
from .monitor_swarm_admin import MonitorSwarmAdminMixin
from .monitor_swarm_graph import MonitorSwarmGraphMixin
from .monitor_swarm_robots import MonitorSwarmRobotsMixin
from .monitor_system_actor import MonitorSystemActorMixin
from .monitor_team_comms import MonitorTeamCommsMixin
from .monitor_team_swarm import MonitorTeamSwarmMixin
from .monitor_teams import MonitorTeamsMixin
from .monitor_ticks import MonitorTicksMixin
from .monitor_whatsapp_inbound import MonitorWhatsappInboundMixin
from .monitor_whatsapp_outbound import MonitorWhatsappOutboundMixin
from .notifier import Notifier
from .openclaw_chat import build_chat_persistence, build_project_memory_store
from .reaper import Reaper
from .recovery import Recovery
from .state import MonitorState
from .storage import bootstrap_accounts, init_whatsapp_allowlist
from .tracker import AgentTracker
from .whatsapp_outbound import ensure_whatsapp_outbound_worker

log = logging.getLogger(__name__)


class GoisMonitor(
    MonitorAuthMixin,
    MonitorTeamsMixin,
    MonitorTeamSwarmMixin,
    MonitorTeamCommsMixin,
    MonitorHermesProfilesMixin,
    MonitorHermesCatalogMixin,
    MonitorHermesAgentCreateMixin,
    MonitorSwarmGraphMixin,
    MonitorSwarmRobotsMixin,
    MonitorSwarmAdminMixin,
    MonitorWhatsappOutboundMixin,
    MonitorWhatsappInboundMixin,
    MonitorLatexMixin,
    MonitorAttendanceMixin,
    MonitorOpenclawSkillsMixin,
    MonitorActiveAgentsMixin,
    MonitorCronConcurrencyMixin,
    MonitorHermesCronPathsMixin,
    MonitorHermesRecoveryMixin,
    MonitorHermesCronDiagnoseMixin,
    MonitorKnowledgeMixin,
    MonitorHermesCronHandlersMixin,
    MonitorHermesCronActionsMixin,
    MonitorHermesCronForceRemoveMixin,
    MonitorHermesCronResultsMixin,
    MonitorHermesKanbanMixin,
    MonitorHermesKanbanHandlersMixin,
    MonitorOpenclawChatStatusMixin,
    MonitorOpenclawSessionsMixin,
    MonitorOpenclawSendJobsMixin,
    MonitorOpenclawSendHandlersMixin,
    MonitorRufloMixin,
    MonitorHermesDashboardHelpersMixin,
    MonitorSystemActorMixin,
    MonitorKanbanCronSyncMixin,
    MonitorKanbanCronSchedulingMixin,
    MonitorKanbanCronBoardsMixin,
    MonitorKanbanCronExecutionMixin,
    MonitorKanbanCronDelegateMixin,
    MonitorPriorityQueueMixin,
    MonitorTicksMixin,
    MonitorRecoveryTriggersMixin,
    MonitorRecoveryHermesCronMixin,
    MonitorRecoveryMiscMixin,
    MonitorPersistMixin,
    MonitorLeaderMixin,
    MonitorStartupMixin,
    MonitorStatusMixin,
    MonitorRunBootstrapMixin,
    MonitorHealthLoopsMixin,
    MonitorBackgroundLoopsMixin,
    MonitorHermesKanbanBoardMixin,
    MonitorHermesKanbanScheduleExecMixin,
    MonitorHermesKanbanScheduleCronMixin,
    MonitorHermesKanbanScheduleMixin,
    MonitorHermesCronMixin,
    MonitorModelUsageMixin,
    MonitorModelQuotasMixin,
    MonitorSwarmCronPolicyMixin,
    MonitorEnvKeysMixin,
    MonitorMongodbKeepaliveMixin,
):
    def __init__(self, cfg: Config, metrics: Optional[Metrics] = None):
        self.cfg = cfg

        def _mongo_bootstrap() -> None:
            try:
                from .gois_lite import is_gois_lite
                from .gois_lite_storage import lite_uses_mongo

                if is_gois_lite() and not lite_uses_mongo():
                    log.info("gois-lite SQL storage — skipping mongo bootstrap")
                    return
                from .mongo_persistence import bootstrap_stack_migrations

                bootstrap_stack_migrations(cfg)
            except Exception as exc:
                log.warning("mongo bootstrap skipped: %s", exc)

        threading.Thread(
            target=_mongo_bootstrap, name="mongo-bootstrap", daemon=True
        ).start()
        self.recovery = Recovery(cfg.qclaw)
        self.hermes_recovery: Optional[Recovery] = (
            Recovery(cfg.hermes) if cfg.hermes else None
        )
        self.notifier = Notifier(cfg.notifier)
        self.metrics = metrics
        self.reaper = Reaper(cfg.qclaw, cfg.reaper, hcfg=cfg.hermes)
        # log scanner (may be empty; still kept for status_snapshot consistency)
        self.log_scanner = LogScanner(
            paths=cfg.log_scanner.paths,
            patterns=[LogPattern(p.name, p.pattern) for p in cfg.log_scanner.patterns],
        )
        self.tracker = AgentTracker()
        self.tracker.register(
            "health",
            f"pgrep {cfg.qclaw.process_pattern or cfg.qclaw.name} every "
            f"{int(cfg.monitor.interval_seconds)}s and HTTP-probe if configured",
        )
        self.tracker.register(
            "reaper",
            f"ps every {int(cfg.reaper.interval_seconds)}s; "
            "kill QClaw helpers orphaned when the main process dies",
        )
        self.tracker.register(
            "recovery",
            f"on {cfg.monitor.failure_threshold} consecutive failures or log match, "
            f"ask {cfg.agent.model} to diagnose and (optionally) restart",
        )
        self.tracker.register(
            "log_scanner",
            f"every {int(cfg.log_scanner.interval_seconds)}s, tail "
            f"{len(cfg.log_scanner.paths)} log file(s) for "
            f"{len(cfg.log_scanner.patterns)} error pattern(s)",
        )
        self.tracker.register(
            "openclaw_doctor",
            (
                "run `openclaw doctor --fix` on patterns "
                f"{cfg.openclaw_doctor.trigger_patterns or '(none)'}; "
                f"cooldown {int(cfg.openclaw_doctor.cooldown_seconds)}s, "
                f"timeout {int(cfg.openclaw_doctor.timeout_seconds)}s"
            ),
        )
        if not cfg.openclaw_doctor.enabled:
            self.tracker.get("openclaw_doctor").enabled = False
            self.tracker.get("openclaw_doctor").state = "paused"
        if cfg.hermes:
            self.tracker.register(
                "health_hermes",
                f"pgrep {cfg.hermes.process_pattern or cfg.hermes.name} every "
                f"{int(cfg.monitor.interval_seconds)}s",
            )
        self.tracker.register(
            "hermes_cron_retry",
            (
                "re-run failed Hermes cron jobs via `hermes cron run` on patterns "
                f"{cfg.hermes_cron_recovery.trigger_patterns or '(none)'}; "
                f"cooldown {int(cfg.hermes_cron_recovery.cooldown_seconds)}s, "
                f"timeout {int(cfg.hermes_cron_recovery.timeout_seconds)}s"
            ),
        )
        if not cfg.hermes_cron_recovery.enabled:
            self.tracker.get("hermes_cron_retry").enabled = False
            self.tracker.get("hermes_cron_retry").state = "paused"
        self.tracker.register(
            "model_quota",
            "enforcement de cota diária por modelo (pausa cron ao ultrapassar)",
        )
        if not cfg.hermes:
            self.tracker.get("model_quota").enabled = False
            self.tracker.get("model_quota").state = "paused"
        self.tracker.register(
            "swarm_cron_policy",
            "pausa e bloqueia crons cujo perfil não pertence a um swarm",
        )
        if not cfg.hermes or not cfg.hermes_cron_recovery.swarm_only:
            self.tracker.get("swarm_cron_policy").enabled = False
            self.tracker.get("swarm_cron_policy").state = "paused"
        cc = cfg.hermes_cron_recovery
        self.tracker.register(
            "hermes_cron_gateway",
            (
                f"ensure Hermes gateway for cron jobs at {cc.jobs_path} "
                f"every {int(cc.ensure_gateway_interval_seconds)}s"
            ),
        )
        if not cc.enabled or not cc.ensure_gateway:
            self.tracker.get("hermes_cron_gateway").enabled = False
            self.tracker.get("hermes_cron_gateway").state = "paused"
        csa = cfg.hermes_cron_scheduler_agent
        self.tracker.register(
            "recovery_hermes_cron_scheduler",
            (
                f"agente LLM ({csa.profile_id}) para recuperar cron scheduler "
                f"{'(automático) ' if csa.auto_recover else ''}"
                f"— cooldown {int(csa.cooldown_seconds)}s"
            ),
        )
        if not csa.enabled or not csa.llm_enabled or not cc.enabled:
            self.tracker.get("recovery_hermes_cron_scheduler").enabled = False
            self.tracker.get("recovery_hermes_cron_scheduler").state = "paused"
        cda = cfg.hermes_cron_diagnostic_agent
        self.tracker.register(
            "hermes_cron_diagnostic",
            (
                f"agente LLM ({cda.profile_id}) — diagnóstico cron "
                f"{'(automático se scheduler parado) ' if cda.auto_run_on_scheduler_down else ''}"
                f"(read-only, cooldown {int(cda.cooldown_seconds)}s)"
            ),
        )
        if not cda.enabled or not cda.llm_enabled or not cfg.hermes:
            self.tracker.get("hermes_cron_diagnostic").enabled = False
            self.tracker.get("hermes_cron_diagnostic").state = "paused"
        self.tracker.register(
            "recovery_hermes",
            (
                f"keepalive: restart after "
                f"{cfg.hermes_keepalive.failure_threshold} failures "
                f"(cooldown {int(cfg.hermes_keepalive.cooldown_seconds)}s)"
                if cfg.hermes_keepalive.enabled
                else f"on {cfg.monitor.failure_threshold} failures, ask {cfg.agent.model}"
            ),
        )
        if cfg.hermes:
            hdc = cfg.hermes_dashboard
            self.tracker.register(
                "hermes_dashboard",
                (
                    f"ensure `hermes dashboard` at "
                    f"{cfg.hermes.dashboard_url or 'http://127.0.0.1:9119'} "
                    f"every {int(hdc.interval_seconds)}s"
                ),
            )
            if not hdc.enabled:
                self.tracker.get("hermes_dashboard").enabled = False
                self.tracker.get("hermes_dashboard").state = "paused"
        wd = cfg.whatsapp_digest
        self.tracker.register(
            "whatsapp_digest",
            (
                f"envia status dos agentes via WhatsApp a cada "
                f"{int(wd.interval_seconds)}s"
            ),
        )
        if not wd.enabled:
            self.tracker.get("whatsapp_digest").enabled = False
            self.tracker.get("whatsapp_digest").state = "paused"
        sd = cfg.skill_discovery
        self.tracker.register(
            "skill_discovery",
            (
                f"varre skills novas (bundled, Hermes, ClawHub) a cada "
                f"{int(sd.interval_seconds)}s"
            ),
        )
        if not sd.enabled:
            self.tracker.get("skill_discovery").enabled = False
            self.tracker.get("skill_discovery").state = "paused"
        if wd.recipient:
            ensure_whatsapp_outbound_worker()
        self._whatsapp_sync_proc: Optional[asyncio.subprocess.Process] = None
        # --- WhatsApp allowlist + accounts (MongoDB, auto-seed from legacy once) ---
        self.allowlist_store = init_whatsapp_allowlist(
            recipient=wd.recipient,
            allowed_recipients=list(
                (wd.allowed_recipients or [])
                + (wd.cron_allowed_recipients or [])
            ),
        )
        self.accounts = bootstrap_accounts(cfg.auth)
        from .storage import (
            repair_team_whatsapp_group_links,
            sync_team_whatsapp_groups_to_allowlist,
        )

        repair_team_whatsapp_group_links(self.accounts)
        sync_team_whatsapp_groups_to_allowlist(self.accounts)
        self._processes_cache: list = []
        self._processes_cache_ts: float = 0.0
        self._state_path: Optional[Path] = (
            Path(cfg.state.path) if cfg.state.path else None
        )
        self.state = MonitorState.load(self._state_path)
        from .runtime_integrity import bootstrap_integrity_state

        bootstrap_integrity_state()

        # WhatsApp group message store (per-team message history, MongoDB).
        from .team_group_messages import TeamGroupMessageStore

        _msg_db = Path(cfg.auth.data_dir).expanduser().resolve() / "group_messages.db"
        self.group_message_store = TeamGroupMessageStore(_msg_db)
        from .team_email_messages import TeamEmailMessageStore

        _email_msg_db = (
            Path(cfg.auth.data_dir).expanduser().resolve() / "team_email_messages.db"
        )
        self.team_email_message_store = TeamEmailMessageStore(_email_msg_db)
        if not isinstance(self.state.model_daily_token_quotas, dict):
            self.state.model_daily_token_quotas = {}
        if not isinstance(self.state.model_daily_usd_quotas, dict):
            self.state.model_daily_usd_quotas = {}
        if not isinstance(self.state.model_usd_per_1k_prices, dict):
            self.state.model_usd_per_1k_prices = {}
        if self.state.model_daily_quota_block is not None and not isinstance(
            self.state.model_daily_quota_block, dict
        ):
            self.state.model_daily_quota_block = None
        # restore byte offsets so a monitor restart doesn't replay old matches
        if self.state.log_scanner_offsets:
            self.log_scanner.offsets = dict(self.state.log_scanner_offsets)
        else:
            self.log_scanner.initialize_offsets()
        # per-process safety cap; resets each run, intentionally not persisted
        self._recovery_attempts = 0
        self._hermes_recovery_attempts = 0
        self._hermes_cron_scheduler_recovery_attempts = 0
        self._hermes_cron_diagnostic_attempts = 0
        self._hermes_last_ok: Optional[bool] = None
        self._hermes_dashboard_up: Optional[bool] = None
        self._hermes_dashboard_last_start_ts: float = 0.0
        self._hermes_profiles_cache_expires_at: float = 0.0
        self._hermes_profiles_cache_enriched: bool = False
        self._hermes_profiles_cache_all: list[dict] = []
        self._hermes_profiles_cache_by_user: dict[str, list[dict]] = {}
        self._role_catalog_seed_status: dict[str, Any] = {"running": False}
        # Set once the startup role-catalog seed has run, so the supervised
        # leader-loop idles instead of busy-spinning a re-seed every few seconds.
        self._role_catalog_seeded: bool = False
        self._hermes_cron_cache_expires_at: float = 0.0
        self._hermes_cron_cache_snapshot: Optional[dict[str, Any]] = None
        self._hermes_cron_cache_lock = threading.Lock()
        self._swarm_robots_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._swarm_robots_cache_lock = threading.Lock()
        self._hermes_cron_token_stats_cache_expires_at: float = 0.0
        self._hermes_cron_token_stats_cache: Optional[dict[str, dict[str, Any]]] = None
        self._hermes_cron_token_stats_cache_lock = threading.Lock()
        self._hermes_cron_token_stats_refreshing: bool = False
        self._model_daily_usage_cache_expires_at: float = 0.0
        self._model_daily_usage_cache_day: Optional[str] = None
        self._model_daily_usage_cache: Optional[dict[str, Any]] = None
        self._model_daily_usage_cache_lock = threading.Lock()
        self._recurring_cron_repair_ts: float = 0.0
        self._swarm_only_cron_enforce_ts: float = 0.0
        self._hermes_cron_scheduler_probe: Optional[dict[str, Any]] = None
        self._hermes_cron_diagnostic_cache: Optional[dict[str, Any]] = None
        self._openclaw_tools_cache_expires_at: float = 0.0
        self._openclaw_tools_cache: Optional[dict[str, Any]] = None
        self.init_status_cache()
        self.init_leader_lock()
        self._active_agents_cache_expires_at: float = 0.0
        self._active_agents_cache_snapshot: Optional[dict[str, Any]] = None
        self._active_agents_cache_lock = threading.Lock()
        self._active_agents_refreshing: bool = False
        self.init_ruflo_cache()
        # kanban cron sync: tracks last seen last_status per job_id to detect changes
        self._kanban_cron_job_last_status: dict[str, str] = {}
        # auto-start: avoid re-enqueue spam for the same doing card
        self._kanban_auto_start_cooldown: dict[str, float] = {}
        self._kanban_auto_start_last_scan: float = 0.0
        self.chat_persistence: Optional[ChatPersistence] = (
            build_chat_persistence(cfg.openclaw_chat)
            if cfg.openclaw_chat.enabled and cfg.openclaw_chat.history_enabled
            else None
        )
        self.project_memory = (
            build_project_memory_store(cfg.openclaw_chat)
            if cfg.openclaw_chat.enabled and cfg.openclaw_chat.project_memory_enabled
            else None
        )
        if cfg.openclaw_chat.enabled and cfg.openclaw_chat.async_send:
            from .chat_send_queue import configure_global_chat_send_limit

            configure_global_chat_send_limit(cfg.openclaw_chat.max_parallel_chat_sends)
            jobs_db = (
                Path(cfg.openclaw_chat.history_db_path).expanduser().resolve().parent
                / "send_jobs.sqlite3"
            )
            init_chat_jobs_persistence(jobs_db)
        self._knowledge_store: Optional[KnowledgeStore] = None
        if cfg.openclaw_chat.enabled and cfg.openclaw_chat.history_enabled:
            try:
                self._knowledge_store = KnowledgeStore(
                    knowledge_default_store_path(cfg.openclaw_chat.history_db_path)
                )
            except OSError as exc:
                log.warning("knowledge_base: failed to init store: %s", exc)
                self._knowledge_store = None
        self._sync_metrics_from_state()
        # Alert engine — real-time notification for critical events
        default_rules = default_alert_rules() if cfg.alerting.enabled and not cfg.alerting.rules else []
        effective_rules = cfg.alerting.rules or default_rules
        merged_cfg = cfg.alerting.model_copy(update={"rules": effective_rules}) if default_rules else cfg.alerting
        self.alert_engine = AlertEngine(merged_cfg, whatsapp_recipient=cfg.whatsapp_digest.recipient)
        self.tracker.register(
            "alerting",
            (
                f"avalia {len(effective_rules)} regras de alerta a cada "
                f"{int(merged_cfg.interval_seconds)}s"
            ),
        )
        if not merged_cfg.enabled:
            self.tracker.get("alerting").enabled = False
            self.tracker.get("alerting").state = "paused"

        integrity_interval = max(60.0, float(merged_cfg.interval_seconds) * 2)
        self.tracker.register(
            "runtime_integrity",
            (
                f"reconcilia Mongo/Redis ↔ JSON quando há split-brain "
                f"(a cada {int(integrity_interval)}s)"
            ),
        )
        sm = cfg.swarm_memory
        mem_interval = max(300.0, float(getattr(sm, "db_maintenance_interval_seconds", 3600.0) or 3600.0))
        self.tracker.register(
            "ruflo_memory_maintenance",
            (
                f"checkpoint WAL + backup de .swarm/memory.db "
                f"(a cada {int(mem_interval)}s)"
            ),
        )
        if not getattr(sm, "db_maintenance_enabled", True):
            self.tracker.get("ruflo_memory_maintenance").enabled = False
            self.tracker.get("ruflo_memory_maintenance").state = "paused"

        mkc = cfg.mongodb_keepalive
        self.tracker.register(
            "mongodb_keepalive",
            (
                f"ping MongoDB a cada {int(mkc.interval_seconds)}s; "
                f"restart via brew services se offline "
                f"(cooldown {int(mkc.cooldown_seconds)}s, max {mkc.max_restart_attempts} tentativas)"
            ),
        )
        if not mkc.enabled:
            self.tracker.get("mongodb_keepalive").enabled = False
            self.tracker.get("mongodb_keepalive").state = "paused"
        self._mongodb_restart_attempts: int = 0
        self._mongodb_last_start_ts: float = 0.0

        def _deferred_startup_tail() -> None:
            self.init_priority_queue()
            try:
                from .runtime_integrity import bootstrap_runtime_reconciliation

                bootstrap_runtime_reconciliation(self._runtime_legacy_paths())
            except Exception as exc:
                log.warning("startup runtime reconcile skipped: %s", exc)

        threading.Thread(
            target=_deferred_startup_tail, name="monitor-deferred-init", daemon=True
        ).start()
