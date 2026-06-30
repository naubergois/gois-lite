from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# MongoDB collection + default document key for app configuration.
CONFIG_COLLECTION = "config"
CONFIG_DEFAULT_KEY = "default"


def _pytest_running() -> bool:
    import os

    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _config_mongo_write_allowed(*, source_path: Optional[str] = None) -> bool:
    """Return False when a test would overwrite the live ``gois`` config."""
    import os

    from .mongo import DEFAULT_DB, mongo_db_name

    if os.environ.get("QCLAW_ALLOW_CONFIG_MONGO_WRITE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return True
    if not _pytest_running():
        return True
    if mongo_db_name() != DEFAULT_DB:
        return True
    if source_path:
        lowered = str(source_path).lower()
        if "/pytest-of-" in lowered or "/pytest_cache/" in lowered:
            return False
    return False


def _guard_config_mongo_write(*, source_path: Optional[str] = None) -> None:
    import logging

    from .mongo import DEFAULT_DB

    log = logging.getLogger(__name__)
    if _config_mongo_write_allowed(source_path=source_path):
        return
    msg = (
        f"refusing to write config to production MongoDB ({DEFAULT_DB!r}) during tests; "
        "use the isolated MONGODB_DB from tests/conftest.py"
    )
    log.warning("%s (source_path=%r)", msg, source_path)
    raise RuntimeError(msg)


class HangCheckConfig(BaseModel):
    # macOS-only: probe the GUI app via AppleScript with a hard timeout.
    # A timed-out probe is treated as a failed health check (hung UI),
    # which is something pgrep alone cannot see.
    enabled: bool = False
    # System Events process name (e.g. "QClaw"). Falls back to QclawConfig.name.
    app_name: Optional[str] = None
    # Max seconds to wait for AppleScript to answer.
    timeout_seconds: float = 3.0
    # Consecutive UI hang probes before the responsive check fails health
    # (debounces transient AppleScript stalls; see Recovery._apply_hang_failure_threshold).
    failure_threshold: int = 2


class QclawConfig(BaseModel):
    name: str = "qclaw"

    # primary health signal: process is alive (matched via pgrep -f)
    process_pattern: Optional[str] = None

    # optional secondary signal: HTTP endpoint must return expected_status
    health_url: Optional[str] = None
    # QClaw writes the live openclaw-gateway port here on startup; when set,
    # overrides the port in health_url so we don't probe a stale fixed port.
    gateway_port_file: Optional[str] = "~/.qclaw-oversea/qclaw.json"
    expected_status: int = 200
    timeout_seconds: float = 5.0

    # optional tertiary signal: GUI responsiveness via AppleScript probe.
    hang_check: HangCheckConfig = Field(default_factory=HangCheckConfig)

    start_command: Optional[list[str]] = None
    stop_command: Optional[list[str]] = None
    working_dir: Optional[str] = None

    log_paths: list[str] = Field(default_factory=list)

    # Optional link to the service's own web UI (e.g. Hermes dashboard).
    dashboard_url: Optional[str] = None

    @model_validator(mode="after")
    def _at_least_one_signal(self) -> "QclawConfig":
        if not self.process_pattern and not self.health_url:
            raise ValueError(
                "qclaw: configure at least one of process_pattern / health_url"
            )
        return self


class LeaderElectionConfig(BaseModel):
    """Ensure a single monitor instance runs mutating background loops."""

    enabled: bool = True
    lease_seconds: float = 30.0
    renew_interval_seconds: float = 10.0
    lock_key: str = "monitor:leader"
    file_lock_name: str = ".monitor-leader.lock"


class MonitorConfig(BaseModel):
    interval_seconds: float = 30.0
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    max_recovery_attempts: int = 5
    leader_election: LeaderElectionConfig = Field(default_factory=LeaderElectionConfig)


class DashboardRenderConfig(BaseModel):
    """Frontend monitor table rendering tuning (chunked/incremental paint)."""

    dynamic_chunking: bool = True
    frame_budget_ms: float = 9.0
    base_chunk_size: int = 40
    agents_chunk_size: int = 35
    ruflo_agents_chunk_size: int = 30
    cron_chunk_size: int = 25
    # UI polling intervals (milliseconds)
    status_poll_ms: int = 15000
    ruflo_poll_ms: int = 15000
    swarm_poll_ms: int = 20000


class SwarmRobotsConfig(BaseModel):
    """Cache and SSE streaming for GET /swarm/robots (panel latency)."""

    cache_seconds: float = 20.0
    # Incremental SSE stream (/swarm/robots/stream) for faster first paint.
    sse_enabled: bool = True
    sse_chunk_size: int = 8


class AgentConfig(BaseModel):
    # OpenAI-compatible client config. Defaults target DeepSeek, but any
    # OpenAI-compatible endpoint works.
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"
    timeout_seconds: float = 90.0
    max_tool_iterations: int = 1000
    system_prompt: str = (
        "You are an SRE agent responsible for diagnosing and recovering the qclaw service."
    )


class LLMGatewayConfig(BaseModel):
    """Fase 0: gateway/observabilidade opcional para chamadas LLM.

    Não-disruptivo: quando ``enabled`` é falso, os clientes OpenAI são criados
    exatamente como antes. Quando ligado, todas as chamadas de chat completion
    passam a registar latência/tokens/custo num ledger JSONL local e, se
    configurado, num projeto Langfuse. Um proxy LiteLLM pode ser interposto para
    fallback/budget/rate-limit sem alterar os call sites.
    """

    # Master switch — liga tracing/usage ledger + override de gateway.
    enabled: bool = False

    # ── LiteLLM (proxy/gateway) ────────────────────────────────────────────
    # Quando ligado e ``litellm_base_url`` definido, todos os clientes apontam
    # para o proxy LiteLLM (que cuida de fallback/budget/logging de custo).
    litellm_enabled: bool = False
    litellm_base_url: Optional[str] = None
    litellm_api_key_env: str = "LITELLM_MASTER_KEY"

    # ── Langfuse (tracing) ─────────────────────────────────────────────────
    langfuse_enabled: bool = False
    # Usa o wrapper drop-in ``langfuse.openai`` (auto custo/latência). Quando
    # falso e langfuse_enabled, usa o SDK manual como sink secundário.
    langfuse_dropin: bool = True
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_public_key_env: str = "LANGFUSE_PUBLIC_KEY"
    langfuse_secret_key_env: str = "LANGFUSE_SECRET_KEY"
    # Injeta tags (name/session_id/metadata/tags) nas chamadas para associar a
    # trace ao agente/swarm. Desligue se a versão do Langfuse rejeitar kwargs.
    langfuse_tag_injection: bool = True

    # ── Ledger local (sempre verificável, offline) ─────────────────────────
    usage_log_enabled: bool = True
    usage_log_path: str = "./.stack/observability/llm_usage.jsonl"
    # Máximo de chars do prompt/resposta gravados no ledger (0 = não gravar texto).
    usage_log_preview_chars: int = 0


class SwarmMemoryConfig(BaseModel):
    """Fase 2: blackboard de memória compartilhada entre nós do swarm.

    Não-disruptivo: quando ``enabled`` é falso, o grafo executável (Fase 1)
    continua passando o histórico completo de entregas ao próximo nó. Quando
    ligado, cada nó grava a sua entrega num blackboard persistente e os nós
    seguintes recuperam apenas o contexto relevante (top-K) em vez de reler o
    card inteiro.

    O backend ``local`` (padrão) usa um ledger JSONL por swarm e funciona
    offline/sem dependências. ``mem0`` e ``letta`` são opcionais (recuperação
    semântica) e exigem os pacotes correspondentes.
    """

    # Master switch — liga a leitura/escrita no blackboard durante a execução.
    enabled: bool = False

    # backend: "local" | "mem0" | "letta" | "agentdb"
    backend: str = "local"

    # Quantas memórias relevantes injetar no contexto de cada nó.
    retrieval_top_k: int = 5

    # ── Backend local (JSONL por swarm) ────────────────────────────────────
    # Diretório base; o store fica em <store_dir>/<swarm>/blackboard.jsonl.
    store_dir: str = "./.stack/swarms"
    # Máx. de chars guardados por entrada (0 = sem corte).
    max_entry_chars: int = 8000

    # ── Mem0 (opcional, recuperação semântica) ─────────────────────────────
    mem0_api_key_env: str = "MEM0_API_KEY"
    # Quando definido, usa o Mem0 self-host/OSS em vez do serviço cloud.
    mem0_base_url: Optional[str] = None

    # ── Letta / MemGPT (opcional) ──────────────────────────────────────────
    letta_base_url: Optional[str] = None
    letta_api_key_env: str = "LETTA_API_KEY"

    # ── AgentDB / RuFlo (opcional) ─────────────────────────────────────────
    # Caminho para o SQLite do RuFlo (``memory init``). Cada swarm usa um
    # namespace ``swarm:<nome>`` em ``memory_entries``.
    agentdb_path: str = "./.swarm/memory.db"
    # Hardening for RuFlo AgentDB (``.swarm/memory.db``).
    db_lock_enabled: bool = True
    db_min_free_mb: int = 512
    db_maintenance_enabled: bool = True
    db_maintenance_interval_seconds: float = 3600.0
    db_backup_keep: int = 3


class SwarmRufloHooksConfig(BaseModel):
    """Integração Fase 2: hooks RuFlo após cada nó do grafo Hermes.

    Quando ligado, cada nó dispara ``memory_store``, ``hooks_post-task`` e,
    opcionalmente, ``hooks_route`` para sugerir o próximo agente em handoffs
    condicionais. Escritas directas no AgentDB são sempre tentadas; a CLI
    RuFlo é best-effort quando ``use_cli`` está activo.
    """

    enabled: bool = False
    # Fast swarms: skip CLI memory_store/route; keep local pattern DB only.
    light: bool = False
    memory_store: bool = True
    post_task: bool = True
    route: bool = True
    use_cli: bool = True
    transport: str = "auto"
    mcp_timeout_seconds: float = 30.0
    cli_timeout_seconds: float = 15.0
    agentdb_path: str = "./.swarm/memory.db"
    project_dir: Optional[str] = None
    ruflo_bin: str = "npx"
    ruflo_args: list[str] = Field(default_factory=lambda: ["-y", "ruflo@latest"])


class SwarmRufloCoordinatorConfig(BaseModel):
    """Integração Fase 3: RuFlo coordena, Hermes executa (kanban / team swarm).

    Antes de ``run_team_swarm``, o RuFlo faz ``swarm start`` (opcional) e
    ``hooks_route`` para sugerir a ordem dos agentes Hermes. A execução LLM
    continua no grafo Hermes; resultados voltam ao AgentDB via Fase 1–2.
    """

    enabled: bool = False
    start_swarm: bool = True
    route_before_run: bool = True
    reorder_agents: bool = True
    strategy: str = "development"
    cli_timeout_seconds: float = 20.0
    agentdb_path: str = "./.swarm/memory.db"
    project_dir: Optional[str] = None
    ruflo_bin: str = "npx"
    ruflo_args: list[str] = Field(default_factory=lambda: ["-y", "ruflo@latest"])


class SwarmRufloEngineConfig(BaseModel):
    """Integração Fase 4–6: motor RuFlo V3, executor Hermes, MCP, HITL e A2A."""

    enabled: bool = False
    default_engine: str = "hermes"  # hermes | ruflo
    fallback_to_hermes: bool = True
    use_hermes_executor: bool = True
    topology: str = "hierarchical-mesh"
    max_agents: int = 15
    strategy: str = "specialized"
    task_type: str = "development"
    init_before_run: bool = True
    parallel_start: bool = True
    stop_after_run: bool = False
    route_before_run: bool = True
    cli_timeout_seconds: float = 45.0
    spawn_timeout_seconds: float = 180.0
    agentdb_path: str = "./.swarm/memory.db"
    project_dir: Optional[str] = None
    ruflo_bin: str = "npx"
    ruflo_args: list[str] = Field(default_factory=lambda: ["-y", "ruflo@latest"])
    memory_store: bool = True
    post_task: bool = True
    route: bool = True
    use_cli: bool = True
    transport: str = "auto"  # mcp | cli | auto
    mcp_timeout_seconds: float = 60.0
    hitl_enabled: bool = False


class SwarmRufloA2AConfig(BaseModel):
    """Fase 6: delegação A2A inter-swarm após execução local."""

    enabled: bool = False
    delegate_after_run: bool = True
    peer_agent_urls: list[str] = Field(default_factory=list)
    timeout_seconds: float = 120.0
    include_local_summary: bool = True


class SwarmEvalConfig(BaseModel):
    """Fase 3: avaliação de qualidade e regressão da execução de swarms.

    Não-disruptivo: quando ``enabled`` é falso, `run_swarm_graph` continua a
    devolver apenas a execução. Quando ligado, cada execução do grafo recebe um
    relatório de qualidade (0–100 por nó + geral, faixa A–D), reprova handoffs
    abaixo do limiar e compara com a execução anterior para detetar regressão.

    O backend ``local`` (padrão) é heurístico, offline e determinístico.
    ``deepeval`` é opcional (avaliação semântica via LLM) com fallback ao local.
    """

    # Master switch — liga a avaliação após cada execução do grafo.
    enabled: bool = False

    # backend: "local" | "deepeval"
    backend: str = "local"

    # Limiar geral para aprovar a execução (0–100).
    threshold: float = 70.0
    # Piso por nó: um único handoff abaixo disto reprova a execução.
    node_floor: float = 55.0
    # Tamanho mínimo de saída por nó (chars) para pontuar "substância" cheia.
    min_chars: int = 80

    # Histórico/regressão.
    store_dir: str = "./.stack/swarms"
    # Queda de pontuação (pontos) face à execução anterior que conta como regressão.
    regression_delta: float = 5.0


class SwarmRoutingConfig(BaseModel):
    """Fase 4: human-in-the-loop + handoff condicional na execução de swarms.

    Não-disruptivo: quando tudo está desligado, o grafo executa como na Fase 1
    (ordem determinística, sem pausas).
    """

    # ── Human-in-the-loop ──────────────────────────────────────────────────
    hitl_enabled: bool = False
    # Nós (nome ou papel) que exigem aprovação humana antes de executar. Também
    # se pode marcar por agente no SwarmSpec (`requires_approval: true`).
    require_approval_for: list[str] = Field(default_factory=list)

    # ── Handoff condicional (router) ───────────────────────────────────────
    conditional_handoff: bool = False
    # router: "llm" (escolhe via modelo) | "first" (determinístico, 1º candidato)
    router_backend: str = "llm"
    # Limite de passos do motor dinâmico (proteção contra loops).
    max_steps: int = 50

    # Checkpoint ``running`` abandonado (segundos) — após isso libera nova execução.
    # Também configurável via ``QCLAW_SWARM_CHECKPOINT_STALE_SECONDS`` (prioridade).
    checkpoint_stale_seconds: float = 7200.0

    # ── Roteamento custo/qualidade por nó (RouteLLM) ───────────────────────
    # Quando ligado, cada nó do grafo escolhe entre um modelo "forte" (caro) e
    # um "fraco" (barato) conforme a dificuldade estimada da tarefa, poupando
    # custo em passos simples. Backend "routellm" (aprendido, opcional) com
    # fallback para "heuristic" (offline, determinístico).
    cost_quality_routing: bool = False
    cost_router_backend: str = "heuristic"  # heuristic | routellm
    # Limiar de dificuldade (0–1) acima do qual usa o modelo forte.
    cost_threshold: float = 0.5
    # Modelos explícitos (vazio = deixa o model_router escolher por tier).
    strong_model_id: str = ""
    weak_model_id: str = ""
    # Nome do router RouteLLM (ex.: "mf", "bert", "sw_ranking") quando disponível.
    routellm_router: str = "mf"


class NotifierConfig(BaseModel):
    log_file: Optional[str] = None
    webhook_url: Optional[str] = None


class WhatsappDigestConfig(BaseModel):
    """Send a periodic WhatsApp summary of agent / service health."""
    enabled: bool = False
    interval_seconds: float = 3600.0
    # WhatsApp JID, e.g. 558591736779@s.whatsapp.net
    recipient: Optional[str] = None
    # Extra DM targets allowed for cron agents (legacy key: cron_allowed_recipients).
    allowed_recipients: list[str] = Field(default_factory=list)
    cron_allowed_recipients: list[str] = Field(default_factory=list)
    # Optional wrapper (e.g. wacli-send.sh). Recipient is appended; message via stdin.
    send_command: Optional[list[str]] = None
    timeout_seconds: float = 30.0
    # wacli auth / QR (chat tool qclaw_wacli_auth_qr)
    wacli_bin: Optional[str] = None
    wacli_store_dir: Optional[str] = None
    wacli_auth_timeout_seconds: float = 25.0
    # Após exibir o QR no chat, aguardar pareamento (segundos).
    wacli_auth_wait_seconds: float = 120.0
    # Fila em background: envios não bloqueiam health/reaper/chat (recomendado).
    async_queue: bool = True
    # Receber mensagens via POST /whatsapp/inbound (webhook do `wacli sync --webhook`).
    inbound_enabled: bool = False
    inbound_webhook_secret: Optional[str] = None
    # Remetente permitido (padrão: mesmo que recipient).
    inbound_allowed_sender: Optional[str] = None
    # Sessão do chat DeepSeek para responder (auto: agent:main:whatsapp:dm:<número>).
    inbound_session_key: Optional[str] = None
    inbound_auto_reply: bool = True
    # Inicia `wacli sync --follow --webhook …` junto com o monitor.
    inbound_sync_enabled: bool = False
    # Bypass da allowlist — usado internamente para envios ao grupo WhatsApp do time.
    skip_allowlist: bool = False
    # Bypass do guard de contexto (digest monitor, alertas, testes wacli).
    skip_context_guard: bool = False
    # Bloqueia envios fora do horário comercial (padrão 8h–22h America/Sao_Paulo).
    business_hours_enabled: bool = True
    business_hours_start: int = 8
    business_hours_end: int = 22
    business_hours_timezone: str = "America/Sao_Paulo"


class LogPatternConfig(BaseModel):
    name: str
    pattern: str


class LogScannerConfig(BaseModel):
    enabled: bool = True
    interval_seconds: float = 30.0
    paths: list[str] = Field(default_factory=list)
    patterns: list[LogPatternConfig] = Field(default_factory=list)
    # When a match is found, also kick the recovery agent (bypasses the
    # consecutive-failures threshold but still respects cooldown + cap).
    trigger_recovery: bool = True


class ErrorLogConfig(BaseModel):
    """Dashboard error log page — tail configured logs and monitor failures."""

    enabled: bool = True
    tail_lines_per_file: int = 400
    max_errors: int = 500
    extra_paths: list[str] = Field(default_factory=list)
    include_monitor_events: bool = True


class ReaperConfig(BaseModel):
    enabled: bool = True
    interval_seconds: float = 60.0
    # Python regex matched against each ps `command` field. Default = anything
    # under the QClaw bundle. Make it more permissive at your own risk.
    target_pattern: str = r"/Applications/QClaw\.app/Contents/"
    # When the main qclaw process is gone, kill matching processes (helpers,
    # bridge daemons, …). Z-state zombies are always reported but never killed
    # directly — only their parent receives SIGCHLD.
    kill_orphan_helpers: bool = True
    # Grace before SIGTERM is escalated to SIGKILL.
    sigterm_grace_seconds: float = 2.0
    # Never touch processes younger than this (avoids racing app startup).
    min_age_seconds: float = 5.0


class OpenclawDoctorConfig(BaseModel):
    """Auto-run `openclaw doctor --fix` when specific log patterns are seen.

    Doctor is non-destructive (repairs ~/.openclaw/openclaw.json from .bak,
    etc.), so it's safe to fire on a tight loop — gated only by cooldown and
    a hard wall-clock timeout. The recovery LLM also gets it as a tool so it
    can call it explicitly when diagnosing.
    """
    enabled: bool = False
    # Absolute path to the openclaw wrapper script. Leave null to auto-detect
    # via the QClaw bundle's Application Support directory.
    bin_path: Optional[str] = None
    # Env vars the wrapper requires. Auto-detected siblings of bin_path when
    # left null. Both must resolve before doctor can run.
    node_path: Optional[str] = None
    mjs_path: Optional[str] = None
    # Min seconds between consecutive auto-doctor runs (regardless of trigger).
    cooldown_seconds: float = 300.0
    # Hard kill after this many seconds — doctor occasionally hangs.
    timeout_seconds: float = 120.0
    # When the log scanner matches a pattern with one of these *names* (the
    # `name` field under log_scanner.patterns), fire doctor. Empty = never
    # auto-fire from log scans (LLM agent can still call it on demand).
    trigger_patterns: list[str] = Field(default_factory=list)


class AlertRuleConfig(BaseModel):
    """One alert rule — source, condition, channels, cooldown."""

    name: str
    source: str = "qclaw"  # qclaw | hermes | log_scanner | reaper | openclaw_doctor
    # Condition: "consecutive_failures >= N" | "pattern_matched" | "any_failure"
    condition: str = "consecutive_failures >= 3"
    cooldown_seconds: float = 600.0
    # Notification channels
    whatsapp: bool = True
    webhook: bool = False
    log: bool = True
    # Human-readable description
    description: str = ""


class AlertingConfig(BaseModel):
    """Real-time alert engine — evaluates rules every health cycle."""

    enabled: bool = True
    interval_seconds: float = 60.0
    rules: list[AlertRuleConfig] = Field(default_factory=list)
    # Max alerts per rule per hour (rate limit)
    max_per_hour_per_rule: int = 3


class StateConfig(BaseModel):
    path: Optional[str] = "./gois.state.json"


class SkillDiscoveryConfig(BaseModel):
    """Daily scan for new OpenClaw/Hermes skills and user suggestions."""
    enabled: bool = True
    interval_seconds: float = 86400.0  # 24h
    startup_delay_seconds: float = 180.0
    state_path: str = "./.stack/skill_suggestions/state.json"
    max_suggestions: int = 12
    bundled_repo_skills: bool = True
    bundled_skills_dir: Optional[str] = None  # default: repo skills/
    hermes_recommended: bool = True
    clawhub_enabled: bool = True
    clawhub_base_url: str = "https://clawhub.ai"
    clawhub_search_queries: list[str] = Field(
        default_factory=lambda: [
            "coding",
            "automation",
            "monitoring",
            "whatsapp",
            "cron",
        ]
    )
    clawhub_browse_all: bool = True  # also query ClawHub with "*"
    clawhub_limit_per_query: int = 8
    clawhub_timeout_seconds: float = 20.0
    notify_whatsapp: bool = True
    notify_dashboard: bool = True
    whatsapp_max_items: int = 8


class CronConcurrencyConfig(BaseModel):
    """Hermes cron agent semaphore (scripts/concurrency-gate.mjs in OpenClaw workspace)."""

    enabled: bool = True
    max_concurrent: int = 6
    workspace_dir: Optional[str] = None  # default: OpenClaw workspace from runtime
    slot_ttl_minutes: float = 45.0


class MediaStorageConfig(BaseModel):
    """Writable root for generated media (images, audio, video, ZIPs, previews).

    When ``enabled``, chat artifacts/previews/attachments are stored under
    ``<root_dir>/chat/{artifacts,previews,attachments}`` instead of ``.stack/chat/*``.
    """

    enabled: bool = False
    root_dir: str = "/Volumes/NAUBER/HomeOffload/qclaw-media"
    # When the external volume is not mounted, fall back to ``.stack/chat/*``.
    fallback_to_stack: bool = True


class CacheStorageConfig(BaseModel):
    """Writable root for npm/temp/pip/ruflo caches (offload from internal disk).

    When ``enabled``, subprocess caches go under ``<root_dir>/{npm,tmp,ruflo,...}``
    instead of ``/private/tmp`` or ``~/.npm``. LaunchAgent and monitor startup
    propagate ``NPM_CONFIG_CACHE``, ``TMPDIR``, etc. from here.
    """

    enabled: bool = False
    root_dir: str = "/Volumes/NAUBER/HomeOffload/caches"
    fallback_to_stack: bool = True


class HttpConfig(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 9101


class AuthConfig(BaseModel):
    enabled: bool = True
    data_dir: str = "./.stack/accounts"
    session_ttl_seconds: float = 604800.0
    allow_open_registration: bool = True
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = "admin"
    # true = repõe a senha do admin bootstrap a cada arranque (só dev/recuperação)
    reset_bootstrap_admin_password: bool = False
    # Ao criar um time, cria repositório privado no GitHub (requer `gh auth login`).
    auto_create_team_github_repo: bool = True
    # Organização GitHub; vazio = conta autenticada no `gh` (ou QCLAW_GITHUB_ORG).
    team_github_org: Optional[str] = None


class HermesKeepaliveConfig(BaseModel):
    """Keep the Hermes gateway process running with direct restarts (no LLM)."""
    enabled: bool = True
    # Consecutive failed health checks before `hermes gateway start`.
    failure_threshold: int = 2
    cooldown_seconds: float = 30.0
    max_recovery_attempts: int = 10
    # Log pattern names that trigger an immediate restart (no health gate).
    log_trigger_patterns: list[str] = Field(default_factory=lambda: [
        "hermes_gateway_shutdown",
        "hermes_gateway_stopped",
        "hermes_gateway_exit",
        "hermes_gateway_crash",
    ])


class MongoKeepaliveConfig(BaseModel):
    """Keep the MongoDB server running with automatic restarts."""
    enabled: bool = True
    interval_seconds: float = 180.0
    cooldown_seconds: float = 60.0
    max_restart_attempts: int = 5
    # Consecutive ping failures required before attempting a restart.
    failures_before_restart: int = 2
    # Max seconds to wait for each brew/mongod shell command.
    command_timeout_seconds: float = 120.0
    # Seconds to poll the port after a restart command succeeds.
    startup_wait_seconds: float = 30.0
    # Ordered list of shell commands to try when restarting mongod.
    # Leave empty to use auto-detected brew service + mongod --fork fallback.
    start_commands: list[list[str]] = Field(default_factory=list)


class HermesDashboardConfig(BaseModel):
    """Keep `hermes dashboard` running and expose its URL in gois."""
    enabled: bool = True
    start_command: list[str] = Field(default_factory=lambda: [
        "hermes", "dashboard", "--no-open", "--skip-build",
    ])
    interval_seconds: float = 60.0
    cooldown_seconds: float = 60.0
    # Poll HTTP after spawn until the port accepts connections.
    startup_timeout_seconds: float = 30.0


class HermesAgentCreateConfig(BaseModel):
    """Create Hermes profiles + cron jobs from natural language in the dashboard chat."""
    enabled: bool = True
    # Copy config/.env/SOUL from default profile (recommended).
    clone_from_default: bool = True
    # Create a Hermes cron job after the profile (dev workflow).
    schedule_enabled: bool = True
    default_schedule: str = "every 24h"
    default_workdir: Optional[str] = None
    # Base dir where GitHub project agents clone repos (<projects_root>/<profile>/repo).
    # For "single project" setups, point to ./.stack/hermes/projects.
    projects_root: str = "./.stack/hermes/projects"
    default_github_branch: str = "main"
    kanban_filenames: list[str] = Field(
        default_factory=lambda: ["kanban.yaml", ".hermes/kanban.yaml"]
    )
    default_skills: list[str] = Field(
        default_factory=lambda: [
            "plan",
            "writing-plans",
            "test-driven-development",
            "systematic-debugging",
            "requesting-code-review",
            "subagent-driven-development",
        ]
    )
    skill_categories: list[str] = Field(
        default_factory=lambda: ["software-development"]
    )
    cron_accept_hooks: bool = True
    cron_timeout_seconds: float = 120.0
    # HTTP read timeout for dashboard API (profile clone + SOUL write can be slow).
    dashboard_api_timeout_seconds: float = 180.0
    # Cria perfis Hermes do catálogo (TI, pesquisa, YouTube, dev) após o dashboard subir.
    seed_role_catalog_on_start: bool = True
    seed_role_catalog_use_filesystem: bool = True
    seed_role_catalog_template_profile: Optional[str] = None
    seed_role_catalog_progress_every: int = 25
    # Stagger newly-created cron jobs so they don't all fire on the same
    # minute and pile up tool-iteration budget on OpenClaw. Set step to 0
    # to disable. Base hour is used when converting "every Nh" intervals
    # to clock-aligned cron expressions.
    cron_stagger_minutes: int = 15
    cron_stagger_base_hour: int = 9
    # On monitor startup, rewrite the minute of existing cron jobs that
    # collide on the same minute (e.g. multiple "0 H * * *" entries),
    # via `hermes cron edit`. New jobs are always staggered when step > 0.
    cron_stagger_existing_on_startup: bool = True
    # Pause all active Hermes cron jobs while creating a profile (chat/API),
    # then resume only the jobs that were active before the pause.
    pause_cron_jobs_during_create: bool = True
    # Poll the Hermes dashboard until HTTP + session token work (after pausing crons).
    dashboard_ready_wait_seconds: float = 90.0
    dashboard_ready_poll_seconds: float = 2.0
    # Cache Hermes profile list API responses (avoids re-listing on every /roles poll).
    profiles_cache_seconds: float = 120.0
    # When true, merge each profile's local profile.yaml (slow with 500+ profiles).
    profiles_enrich_local_meta: bool = False
    # When true, authenticated users may use any absolute path as kanban/task workdir
    # (not only team dirs or Hermes project folders). Disable on shared hosts.
    kanban_allow_any_workdir: bool = True
    # Max size per kanban card attachment (browser binary upload + file_path).
    max_kanban_attachment_bytes: int = 209_715_200  # 200 MiB


class ChatModelEntry(BaseModel):
    """One selectable LLM in the dashboard chat."""
    id: str
    label: str
    provider: str = "openai_compat"  # openai_compat | anthropic | codex_cli
    model: str
    base_url: str
    api_key_env: str
    supports_attachments: bool = True
    tools_enabled: bool = True
    coding: bool = False
    group: str = "geral"  # geral | programacao
    # Per-model context char limit (overrides global max_context_chars).
    # 0 = use global default. Useful for models with smaller context windows
    # or strict TPM limits (e.g. OpenAI GPT-4o ≈ 128K tokens ≈ 400K chars).
    max_context_chars: int = 0


class RufloChatConfig(BaseModel):
    """Dashboard chat backed by RuFlo routing/memory + shared LLM (openclaw_chat)."""

    enabled: bool = True
    project_dir: Optional[str] = None
    ruflo_bin: str = "npx"
    ruflo_args: list[str] = Field(default_factory=lambda: ["-y", "ruflo@latest"])
    command_timeout_seconds: float = 45.0
    # HTTP cache for GET /ruflo/status (seconds). Higher = fewer CLI probes.
    status_cache_seconds: float = 5.0
    # Per-probe timeout for swarm/hive status CLI calls.
    status_probe_timeout_seconds: float = 8.0
    # When false, skip hive-mind probe (saves ~8s when workers are unused).
    status_probe_hive: bool = True
    # Alert when probe latency exceeds this (ms); feeds ruflo_health + alerting.
    status_alert_latency_ms: float = 12000.0
    memory_search_enabled: bool = True
    memory_search_limit: int = 5
    hosted_ui_url: str = "https://flo.ruv.io/"
    local_ui_url: str = "http://127.0.0.1:3000/"
    sessions_limit: int = 80
    messages_limit: int = 120
    # Intervalo (em segundos) para avisos periódicos no chat enquanto o job roda.
    user_checkin_interval_seconds: float = 5.0
    system_prompt: str = (
        "You are the RuFlo orchestration assistant inside gois. "
        "You help plan and coordinate multi-agent work: routing, swarm topology, "
        "and patterns from RuFlo memory. Be concise and actionable in Portuguese "
        "when the user writes in Portuguese."
    )

    @model_validator(mode="after")
    def _prefer_local_ruflo_bin(self) -> "RufloChatConfig":
        """Use ~/.local/lib/ruflo-cli when present instead of npx (P0/P1 infra)."""
        local_bin = (
            Path.home() / ".local" / "lib" / "ruflo-cli" / "node_modules" / ".bin" / "ruflo"
        )
        local_js = (
            Path.home()
            / ".local"
            / "lib"
            / "ruflo-cli"
            / "node_modules"
            / "ruflo"
            / "bin"
            / "ruflo.js"
        )
        for candidate in (local_bin, local_js):
            if not candidate.is_file():
                continue
            if self.ruflo_bin in ("npx", "") or self.ruflo_bin.strip() == "npx":
                self.ruflo_bin = str(candidate.resolve())
                self.ruflo_args = []
            break
        return self


class OpenclawChatConfig(BaseModel):
    """Dashboard chat with QClaw / OpenClaw (DeepSeek + skills, or openclaw CLI)."""
    enabled: bool = True
    default_agent: str = "main"
    default_session_key: str = "agent:main:gois-dashboard"
    # deepseek = agent.* LLM + OpenClaw SKILL.md in context; openclaw_cli = `openclaw agent`
    backend: str = "deepseek"
    # Liga/desliga a conexão com o OpenClaw local. Quando False, o chat opera
    # em "modo web": força o backend LLM (deepseek) mesmo que ``backend`` peça
    # openclaw_cli, não faz fallback para o OpenClaw CLI quando o LLM falha, e
    # desabilita as ferramentas que falam com o agente OpenClaw do desktop
    # (ask_qclaw_agent, qclaw_heygen_via_openclaw, qclaw_suno_via_openclaw,
    # qclaw_seedance_via_openclaw).
    openclaw_connection_enabled: bool = True
    # Atalhos locais no chat (widgets criativos, WhatsApp, Trello JSON, foto de perfil)
    # sem passar pelo LLM. Desligado = mensagens vão sempre ao agente.
    fastpath_enabled: bool = True
    default_model_id: str = "deepseek-chat"
    max_attachment_bytes: int = 8_388_608
    # Documentos fixos no contexto da conversa (Docs / ficheiro ao contexto).
    # Maior que max_attachment_bytes — permite PDFs grandes via caminho ou upload.
    max_context_doc_bytes: int = 52_428_800  # 50 MiB

    @field_validator("default_model_id", mode="before")
    @classmethod
    def _default_model_not_v4_flash(cls, value: object) -> str:
        from .chat_models import effective_chat_default_model_id

        return effective_chat_default_model_id(
            str(value).strip() if value is not None else None
        )
    max_attachments: int = 6
    # Anexos do chat são gravados aqui; o modelo recebe o caminho absoluto.
    attachments_temp_dir: str = "./.stack/chat/attachments"
    models: list[ChatModelEntry] = Field(default_factory=list)
    send_timeout_seconds: float = 600.0
    # Return immediately from POST /openclaw/send and process in a background thread.
    async_send: bool = True
    # Cap concurrent LLM chat workers process-wide (per-conversation queue still serializes).
    max_parallel_chat_sends: int = 4
    # Cancel async jobs when no /chat tab reports that conversation as active.
    inactive_job_cancel_enabled: bool = True
    presence_ttl_seconds: float = 90.0
    inactive_job_grace_seconds: float = 60.0
    sessions_limit: int = 80
    messages_limit: int = 120
    sessions_cache_seconds: float = 15.0
    history_limit: int = 40
    # Maximum total chars for all LLM messages (system + history + user).
    # Prevents token overflow on models with limited context (e.g. DeepSeek 1M).
    # 0 = no limit. Default ~800K chars ≈ ~200K tokens safety margin.
    max_context_chars: int = 800_000
    # Token budget for chat prompts: economy | balanced | full | debug.
    # Override runtime: QCLAW_CHAT_TOKEN_MODE. UI: seletor «Tokens» no /chat.
    token_usage_mode: str = "balanced"
    # Used only in full/debug modes (see chat_prompt_policy.MODE_PRESETS).
    max_skills_in_prompt: int = 0
    max_skill_body_chars: int = 12000
    # When true (and monitor passes Recovery), the chat LLM can call tools to
    # probe QClaw health/logs, run shell commands, and forward questions to QClaw.
    qclaw_tools_enabled: bool = True
    max_tool_iterations: int = 20
    # Override do cap de schemas de ferramentas no LLM (None = preset do modo Tokens).
    # 0 = ilimitado (só limite do provider); 8–256 = cap explícito (UI /chat).
    tools_cap_override: Optional[int] = None
    # Run bash commands via qclaw_run_shell instead of telling the user to type them.
    shell_enabled: bool = True
    # Native macOS screen/mouse/keyboard tools (screencapture + cliclick + osascript).
    desktop_control_enabled: bool = True
    desktop_screenshot_max_width: int = 1280
    desktop_screenshot_quality: int = 70
    shell_timeout_seconds: float = 120.0
    shell_max_output_chars: int = 12000
    shell_working_dir: Optional[str] = None
    # Bridge HeyGen Remote MCP via OpenClaw (qclaw_heygen_via_openclaw + skill qclaw-heygen-mcp).
    heygen_mcp_enabled: bool = True
    # Bridge Suno MCP via OpenClaw (qclaw_suno_via_openclaw + skill qclaw-suno-mcp).
    suno_mcp_enabled: bool = True
    # Bridge Seedance MCP via OpenClaw (qclaw_seedance_via_openclaw + skill qclaw-seedance-video).
    seedance_mcp_enabled: bool = True
    # Bridge Runway MCP via OpenClaw (qclaw_runway_via_openclaw + skill qclaw-photo-to-video-mcp).
    runway_mcp_enabled: bool = True
    # Foto→vídeo orchestrator tools (qclaw_photo_to_video_* + skill qclaw-chat-photo-to-video-mcp).
    photo_to_video_enabled: bool = True
    # User-registered external MCP servers (MongoDB → qclaw_mcp__* tools in chat).
    external_mcp_enabled: bool = True
    # Extra SKILL.md roots (merged with openclaw.json skills.load.extraDirs).
    extra_skill_dirs: list[str] = Field(default_factory=list)
    # Banda virtual — API Roteiro Viral (qclaw_virtual_band_* + skill qclaw-chat-roteiro-musica).
    virtual_band_enabled: bool = True
    # Thumbnails RV — Imagen + neurodesign (qclaw_thumbnail_* + skill qclaw-chat-roteiro-thumbnails).
    roteiro_thumbnails_enabled: bool = True
    # MongoDB RV read-only (qclaw_rv_mongo_* + skill qclaw-chat-roteiro-mongo).
    roteiro_mongo_enabled: bool = True
    # Local RV API gateway (qclaw_rv_api_* + skill qclaw-chat-roteiro-api).
    roteiro_api_enabled: bool = True
    # SQLite + ChromaDB history for dashboard chat (primary store for new sessions).
    history_enabled: bool = True
    history_db_path: str = "./.stack/chat/history.sqlite3"
    chroma_enabled: bool = True
    chroma_path: str = "./.stack/chat/chroma"
    context_retrieval_limit: int = 8
    project_memory_enabled: bool = True
    project_memory_path: str = "./.stack/chat/project_memory.json"
    project_memory_max_chars: int = 12000
    # Renomear sessões com título placeholder após N mensagens (user+assistant, sem status).
    auto_title_after_messages: int = 2
    # Banner no /chat quando o histórico se aproxima do limite do modo Tokens.
    history_nudge_enabled: bool = True
    # Neural TTS for "Fala: on" (edge-tts, free). Falls back to browser speech if unavailable.
    tts_edge_enabled: bool = True
    tts_edge_voice: str = "pt-BR-FranciscaNeural"
    tts_max_chars: int = 2500
    system_prompt: str = (
        "You are a QClaw / OpenClaw assistant on the gois dashboard. "
        "Follow the OpenClaw skills below when they apply to the user's task. "
        "You have tools to inspect the live QClaw stack (health, processes, logs, "
        "monitor snapshot), run shell commands on the host (qclaw_run_shell), control "
        "the macOS desktop when qclaw_desktop_* tools are available (screenshot, click, "
        "type, keys, windows), and ask questions to the QClaw OpenClaw agent "
        "(ask_qclaw_agent). "
        "NEVER ask the user to run terminal commands themselves — execute commands "
        "with qclaw_run_shell and summarize stdout/stderr in your reply. "
        "NEVER say you will create, send, list, or run something later — call the "
        "appropriate qclaw_* tool in this turn, then summarize the result. "
        "Use ask_qclaw_agent only when the desktop OpenClaw session/tools are required. "
        "For HeyGen video (official Remote MCP), use qclaw_heygen_via_openclaw when available. "
        "For Suno music (AceDataCloud MCP), use qclaw_suno_via_openclaw when available. "
        "For Seedance AI video (AceDataCloud MCP, incl. Seedance 2.0), use "
        "qclaw_seedance_via_openclaw when available. "
        "For Runway cinematic image-to-video, use qclaw_runway_via_openclaw when available. "
        "For photo-to-video routing (plan + provider), use qclaw_photo_to_video_plan when available. "
        "For Gemini Lyria music (Google), use qclaw_gemini_music_generate when available. "
        "For Grok Imagine images (xAI), use qclaw_grok_imagine_generate when available. "
        "For virtual band batch (Roteiro Viral API), use qclaw_virtual_band_create when available. "
        "For YouTube thumbnails (Roteiro Viral Imagen), use qclaw_thumbnail_generate when available. "
        "Project memory persists facts across all dashboard chats — use qclaw_save_project_note "
        "to store durable project facts (paths, decisions, conventions). "
        "Never store passwords or API secrets in project memory — use qclaw_app_passwords_store "
        "(MongoDB env_keys, /chaves) for Gmail/Google app passwords. "
        "Past context from this project may appear under 'Stored context' or "
        "'Project memory (all chats)' — treat it as ground truth for continuity. "
        "IMPORTANT: When your answer uses one or more OpenClaw skills, start your "
        "response with a hidden tag on the VERY FIRST LINE in the format: "
        "<!--skills:skill-name-1,skill-name-2--> (use the exact skill name/slug). "
        "This tag will be stripped from the visible output but used to track skill usage. "
        "Only include skills you actually follow in the response. "
        "When you need the user to pick between a few choices, end your reply with a hidden "
        "marker: <!--question: {\"question\": \"Prompt?\", \"options\": [{\"label\": \"Shown\", "
        "\"value\": \"text sent when clicked\"}]}-->. When you need a specific typed value, use "
        "<!--question: {\"question\": \"Prompt?\", \"type\": \"input\", \"placeholder\": \"hint\"}-->. "
        "The marker is stripped from the visible text and rendered as clickable buttons or an "
        "input box; only emit it when a follow-up answer is genuinely required. "
        "When the user explicitly asks to attach/upload a file without having attached one, "
        "the chat UI opens an upload dialog instead of sending the request. "
        "Mentioning filenames or paths alone does not trigger upload. "
        "Answer in the user's language; be concise and actionable."
    )


class GoisLiteDatabaseConfig(BaseModel):
    """gois-lite persistence: SQLite by default; PostgreSQL or MongoDB optional."""

    backend: str = "sqlite"
    sqlite_path: str = "./.stack/accounts"
    url: str = ""


class GoisLiteConfig(BaseModel):
    """Strip-down UI: Chat + Kanban only (no Swarm/Monitor tabs, no OpenClaw bridge)."""

    enabled: bool = False
    database: GoisLiteDatabaseConfig = Field(default_factory=GoisLiteDatabaseConfig)


class HermesRecoveryAgentConfig(BaseModel):
    """LLM recovery agent for Hermes (profile + tools beyond gateway keepalive)."""
    enabled: bool = True
    profile_id: str = "hermes-recovery"
    seed_profile_on_start: bool = True
    # When false, only keepalive restarts run (no DeepSeek recovery loop).
    llm_enabled: bool = True
    # After keepalive restart still unhealthy, invoke the LLM agent.
    escalate_after_keepalive_failure: bool = True
    # Skip direct `hermes gateway start` and use the LLM agent only.
    use_llm_instead_of_keepalive: bool = False
    system_prompt: str = (
        "You are the Hermes SRE recovery agent used by gois. "
        "Diagnose gateway, dashboard, and cron failures using the provided tools. "
        "Be conservative: evidence first, restart only when needed. "
        "Final report in Portuguese."
    )


class HermesCronSchedulerAgentConfig(BaseModel):
    """LLM agent focused on Hermes cron scheduler / gateway for jobs_path."""
    enabled: bool = True
    profile_id: str = "hermes-cron-recovery"
    seed_profile_on_start: bool = True
    llm_enabled: bool = True
    # When true, the monitor invokes this agent automatically while the scheduler is down.
    auto_recover: bool = True
    cooldown_seconds: float = 600.0
    max_attempts_per_run: int = 5
    system_prompt: str = (
        "You are the Hermes cron scheduler recovery agent. "
        "Restore cron firing by fixing the gateway for jobs_path HERMES_HOME. "
        "Final report in Portuguese."
    )


class HermesCronDiagnosticAgentConfig(BaseModel):
    """LLM agent for read-only Hermes cron failure analysis."""
    enabled: bool = True
    profile_id: str = "hermes-cron-diagnostic"
    seed_profile_on_start: bool = True
    llm_enabled: bool = True
    cooldown_seconds: float = 300.0
    max_attempts_per_run: int = 8
    auto_run_on_log_match: bool = False
    auto_run_on_scheduler_down: bool = True
    system_prompt: str = (
        "You are the Hermes cron diagnostic agent. "
        "Explain failures using read-only tools; do not restart services. "
        "Final report in Portuguese."
    )


class HermesCronRecoveryConfig(BaseModel):
    """Re-run a failed Hermes cron job when the log scanner sees a failure."""
    enabled: bool = False
    cooldown_seconds: float = 600.0
    timeout_seconds: float = 3600.0
    accept_hooks: bool = True
    jobs_path: str = "./.stack/hermes/cron/jobs.json"
    trigger_patterns: list[str] = Field(default_factory=lambda: ["hermes_cron_failed"])
    # Keep a gateway running for the HERMES_HOME that owns jobs_path (cron tick loop).
    ensure_gateway: bool = True
    ensure_gateway_interval_seconds: float = 120.0
    ensure_gateway_cooldown_seconds: float = 300.0
    # Recompute ``next_run_at`` for recurring jobs on this interval (s).
    recurring_repair_interval_seconds: float = 1800.0
    # Recurring jobs without a plausible schedule after this many hours are stale.
    stale_cron_hours: float = 48.0
    # Pause and block cron jobs whose Hermes profile is not linked to a swarm.
    swarm_only: bool = True


class Config(BaseModel):
    qclaw: QclawConfig
    # Optional second target — Hermes gateway (`hermes gateway run`).
    hermes: Optional[QclawConfig] = None
    hermes_keepalive: HermesKeepaliveConfig = Field(default_factory=HermesKeepaliveConfig)
    hermes_dashboard: HermesDashboardConfig = Field(default_factory=HermesDashboardConfig)
    hermes_agent_create: HermesAgentCreateConfig = Field(
        default_factory=HermesAgentCreateConfig
    )
    hermes_cron_recovery: HermesCronRecoveryConfig = Field(
        default_factory=HermesCronRecoveryConfig
    )
    hermes_cron_scheduler_agent: HermesCronSchedulerAgentConfig = Field(
        default_factory=HermesCronSchedulerAgentConfig
    )
    hermes_cron_diagnostic_agent: HermesCronDiagnosticAgentConfig = Field(
        default_factory=HermesCronDiagnosticAgentConfig
    )
    hermes_recovery_agent: HermesRecoveryAgentConfig = Field(
        default_factory=HermesRecoveryAgentConfig
    )
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    dashboard_render: DashboardRenderConfig = Field(
        default_factory=DashboardRenderConfig
    )
    swarm_robots: SwarmRobotsConfig = Field(default_factory=SwarmRobotsConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    llm_gateway: LLMGatewayConfig = Field(default_factory=LLMGatewayConfig)
    swarm_memory: SwarmMemoryConfig = Field(default_factory=SwarmMemoryConfig)
    swarm_ruflo_hooks: SwarmRufloHooksConfig = Field(default_factory=SwarmRufloHooksConfig)
    swarm_ruflo_coordinator: SwarmRufloCoordinatorConfig = Field(
        default_factory=SwarmRufloCoordinatorConfig
    )
    swarm_ruflo_engine: SwarmRufloEngineConfig = Field(
        default_factory=SwarmRufloEngineConfig
    )
    swarm_ruflo_a2a: SwarmRufloA2AConfig = Field(default_factory=SwarmRufloA2AConfig)
    swarm_eval: SwarmEvalConfig = Field(default_factory=SwarmEvalConfig)
    swarm_routing: SwarmRoutingConfig = Field(default_factory=SwarmRoutingConfig)
    notifier: NotifierConfig = Field(default_factory=NotifierConfig)
    whatsapp_digest: WhatsappDigestConfig = Field(default_factory=WhatsappDigestConfig)
    skill_discovery: SkillDiscoveryConfig = Field(default_factory=SkillDiscoveryConfig)
    log_scanner: LogScannerConfig = Field(default_factory=LogScannerConfig)
    error_log: ErrorLogConfig = Field(default_factory=ErrorLogConfig)
    alerting: AlertingConfig = Field(default_factory=AlertingConfig)
    reaper: ReaperConfig = Field(default_factory=ReaperConfig)
    openclaw_doctor: OpenclawDoctorConfig = Field(default_factory=OpenclawDoctorConfig)
    openclaw_chat: OpenclawChatConfig = Field(default_factory=OpenclawChatConfig)
    ruflo_chat: RufloChatConfig = Field(default_factory=RufloChatConfig)
    gois_lite: GoisLiteConfig = Field(default_factory=GoisLiteConfig)
    cron_concurrency: CronConcurrencyConfig = Field(default_factory=CronConcurrencyConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    media_storage: MediaStorageConfig = Field(default_factory=MediaStorageConfig)
    cache_storage: CacheStorageConfig = Field(default_factory=CacheStorageConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    mongodb_keepalive: MongoKeepaliveConfig = Field(default_factory=MongoKeepaliveConfig)

    @classmethod
    def from_yaml(cls, path: Path) -> "Config":
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)

    # ── MongoDB-backed config ───────────────────────────────────────────────
    @classmethod
    def from_mongo(cls, key: str = CONFIG_DEFAULT_KEY) -> Optional["Config"]:
        """Load config from the Mongo ``config`` collection.

        Returns None when no document exists for ``key`` (so callers can fall
        back to YAML). Connection/driver errors propagate to the caller.
        """
        from .mongo import get_collection

        doc = get_collection(CONFIG_COLLECTION).find_one({"_id": key})
        if not doc:
            return None
        data = {
            k: v
            for k, v in doc.items()
            if k not in ("_id", "_updated_at", "_source_path")
        }
        return cls(**data)

    def save_to_mongo(
        self, key: str = CONFIG_DEFAULT_KEY, *, source_path: Optional[str] = None
    ) -> None:
        """Upsert this config as a single document under ``key``."""
        import time

        from .mongo import get_collection

        _guard_config_mongo_write(source_path=source_path)

        data = self.model_dump(mode="json")
        data["_id"] = key
        data["_updated_at"] = time.time()
        if source_path:
            data["_source_path"] = str(source_path)
        get_collection(CONFIG_COLLECTION).replace_one(
            {"_id": key}, data, upsert=True
        )

    @classmethod
    def load(
        cls,
        path: Optional[Path] = None,
        *,
        key: str = CONFIG_DEFAULT_KEY,
        auto_import: bool = True,
    ) -> "Config":
        """Unified loader: MongoDB first, YAML fallback.

        - If a config document exists in Mongo, it is the source of truth.
        - Otherwise, if ``path`` exists, load YAML and (when ``auto_import``)
          seed it into Mongo so subsequent runs read from the database.
        - Raises FileNotFoundError when neither source is available.
        """
        import logging

        log = logging.getLogger(__name__)

        try:
            cfg = cls.from_mongo(key)
        except Exception as exc:  # mongo down / driver missing → try YAML
            log.warning("could not read config from MongoDB (%s); trying YAML", exc)
            cfg = None
        if cfg is not None:
            return cfg

        if path is not None and Path(path).exists():
            cfg = cls.from_yaml(Path(path))
            if auto_import:
                try:
                    cfg.save_to_mongo(key, source_path=str(Path(path).resolve()))
                    log.info("imported %s into MongoDB config[%s]", path, key)
                except Exception as exc:
                    log.warning("could not seed config into MongoDB: %s", exc)
            return cfg

        raise FileNotFoundError(
            f"no config in MongoDB (key={key!r}) and no YAML at {path!r}"
        )
