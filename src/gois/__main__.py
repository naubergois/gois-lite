from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .config import Config
from .article_md_generator import sections_from_names, write_article_template_file
from .env_util import load_dotenv
from .metrics import Metrics, run_http_server
from .monitor import GoisMonitor
from .env_keys_mongo import load_env_keys_cache
from .secrets_fallback import apply_llm_keys_to_environ


def _run_seed_roles(cfg: Config, categories: list[str] | None) -> int:
    if not cfg.hermes or not cfg.hermes_agent_create.enabled:
        print("hermes_agent_create must be enabled in config", file=sys.stderr)
        return 2
    from .hermes_profiles import seed_team_role_presets

    url = (cfg.hermes.dashboard_url or "http://127.0.0.1:9119").rstrip("/")
    print(f"Seeding role catalog via {url} …", file=sys.stderr)
    hac = cfg.hermes_agent_create
    result = seed_team_role_presets(
        url,
        clone_from_default=hac.clone_from_default,
        only_missing=True,
        categories=categories,
        progress_every=hac.seed_role_catalog_progress_every,
        timeout=hac.dashboard_api_timeout_seconds,
        use_filesystem=hac.seed_role_catalog_use_filesystem,
        template_profile=hac.seed_role_catalog_template_profile,
    )
    created = result.get("created") or []
    skipped = result.get("skipped") or []
    errors = result.get("errors") or []
    print(
        f"done: created={len(created)} skipped={len(skipped)} errors={len(errors)}",
        file=sys.stderr,
    )
    if result.get("error"):
        print(f"error: {result['error']}", file=sys.stderr)
    if errors:
        for err in errors[:10]:
            print(f"  error {err.get('preset_id')}: {err.get('error')}", file=sys.stderr)
    return (
        0
        if result.get("ok", True) and not errors and not result.get("error")
        else 1
    )


def _raise_nofile_limit(*, min_soft: int = 65536) -> None:
    """Raise RLIMIT_NOFILE soft limit (launchd default is often 256)."""
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft >= min_soft:
            return
        target = min_soft
        if hard not in (resource.RLIM_INFINITY, -1):
            target = min(min_soft, hard)
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        logging.getLogger(__name__).info(
            "raised RLIMIT_NOFILE soft limit %d → %d", soft, target
        )
    except Exception as exc:
        logging.getLogger(__name__).debug("RLIMIT_NOFILE bump skipped: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gois",
        description="Health-check qclaw and recover via a Claude SDK agent on failure.",
    )
    parser.add_argument(
        "--config", "-c", type=Path, default=Path("config.yaml"),
        help="path to YAML config (default: ./config.yaml)",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--env-file", type=Path, default=Path(".env"),
        help="path to .env (default: ./.env; missing file is OK)",
    )
    sub = parser.add_subparsers(dest="command")
    seed_parser = sub.add_parser(
        "seed-roles",
        help="create Hermes profiles from the role catalog (TI, pesquisa, YouTube, dev)",
    )
    seed_parser.add_argument(
        "--categories",
        nargs="*",
        default=None,
        help="e.g. operacoes-ti pesquisa-cientifica youtube (default: all)",
    )
    article_parser = sub.add_parser(
        "article-md",
        help="gerar template de artigo em Markdown com objetivo e checklist por seção",
    )
    article_parser.add_argument(
        "--title",
        default="Título do Artigo",
        help="título do artigo",
    )
    article_parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("artigo-template.md"),
        help="arquivo de saída .md (default: ./artigo-template.md)",
    )
    article_parser.add_argument(
        "--sections",
        default="",
        help=(
            "lista customizada de seções separadas por vírgula. "
            "Exemplo: Introdução,Fundamentação,Método"
        ),
    )
    article_parser.add_argument(
        "--force",
        action="store_true",
        help="sobrescreve o arquivo de saída se já existir",
    )
    args = parser.parse_args()

    if args.command == "article-md":
        names = [p.strip() for p in str(args.sections or "").split(",") if p.strip()]
        sections = sections_from_names(names) if names else None
        try:
            out = write_article_template_file(
                args.output,
                title=args.title,
                sections=sections,
                overwrite=bool(args.force),
            )
        except FileExistsError as exc:
            print(str(exc), file=sys.stderr)
            print("Use --force para sobrescrever.", file=sys.stderr)
            sys.exit(2)
        print(out)
        sys.exit(0)

    n = load_dotenv(args.env_file)
    if n:
        print(f"loaded {n} var(s) from {args.env_file}", file=sys.stderr)

    # API RV em subprocesso — evita carregar FastAPI in-process no monitor (contenção com chat).
    os.environ.setdefault("QCLAW_RV_SUBPROCESS_ONLY", "1")

    try:
        cached = load_env_keys_cache()
        if cached:
            print(
                f"loaded {len(cached)} env key(s) from MongoDB into memory",
                file=sys.stderr,
            )
    except Exception as exc:
        logging.getLogger(__name__).debug("env keys cache preload skipped: %s", exc)

    key = apply_llm_keys_to_environ(local_env=args.env_file)
    if key and not n:
        print(
            "LLM API keys loaded from sibling project(s) (OpenClaw / Hermes / .env)",
            file=sys.stderr,
        )

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    _raise_nofile_limit()

    # Source of truth is MongoDB; config.yaml is the fallback/seed source.
    try:
        cfg = Config.load(args.config)
    except FileNotFoundError:
        print(
            f"config not found in MongoDB or at {args.config}",
            file=sys.stderr,
        )
        print(
            "Run `python -m gois.scripts.migrate_config_to_mongo` and "
            "`python -m gois.scripts.migrate_env_keys_to_mongo`, or copy "
            "config.example.yaml to config.yaml and edit before running.",
            file=sys.stderr,
        )
        sys.exit(2)

    from .llm_gateway import configure as configure_llm_gateway

    configure_llm_gateway(cfg.llm_gateway)

    from .gois_lite import configure_gois_lite

    configure_gois_lite(cfg)

    from .local_paths import _repo_root
    from .whatsapp_allowlist import apply_whatsapp_cron_guard_environment

    apply_whatsapp_cron_guard_environment(
        project_dir=_repo_root(),
        config_path=args.config.resolve(),
    )

    from .cache_paths import apply_cache_env_to_process

    cache_root = apply_cache_env_to_process()
    logging.getLogger(__name__).info("cache storage root: %s", cache_root)

    if args.command == "seed-roles":
        sys.exit(_run_seed_roles(cfg, args.categories))

    metrics = Metrics() if cfg.http.enabled else None
    monitor = GoisMonitor(cfg, metrics=metrics)
    hermes_dashboard_url = None
    if cfg.hermes:
        hermes_dashboard_url = (
            cfg.hermes.dashboard_url or "http://127.0.0.1:9119"
        )
        log = logging.getLogger(__name__)
        log.info(
            "hermes dashboard link: %s (auto-start=%s)",
            hermes_dashboard_url,
            cfg.hermes_dashboard.enabled,
        )
    if cfg.openclaw_chat.enabled:
        import threading

        def _bootstrap_llm_catalog() -> None:
            try:
                from .llm_models_catalog import ensure_catalog_synced

                ensure_catalog_synced(force=True)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "llm catalog bootstrap skipped: %s", exc
                )

        threading.Thread(target=_bootstrap_llm_catalog, daemon=True).start()
    if metrics:
        run_http_server(
            cfg.http.host,
            cfg.http.port,
            metrics,
            status_provider=monitor.handle_status_snapshot,
            auth_bootstrap=monitor.handle_auth_bootstrap,
            auth_user_from_token=monitor.auth_user_from_token,
            auth_register=monitor.handle_auth_register,
            auth_login=monitor.handle_auth_login,
            auth_me=monitor.handle_auth_me,
            auth_logout=monitor.handle_auth_logout,
            users_list=monitor.handle_users_list,
            users_create=monitor.handle_users_create,
            users_delete=monitor.handle_users_delete,
            users_update=monitor.handle_users_update,
            teams_list=monitor.handle_teams_list,
            teams_presets=monitor.handle_teams_presets,
            team_create=monitor.handle_team_create,
            team_update=monitor.handle_team_update,
            team_delete=monitor.handle_team_delete,
            team_members_add=monitor.handle_team_members_add,
            team_members_remove=monitor.handle_team_members_remove,
            team_members_list=monitor.handle_team_members_list,
            team_contacts_get=monitor.handle_team_contacts_get,
            team_contacts_upsert=monitor.handle_team_contacts_upsert,
            team_contacts_remove=monitor.handle_team_contacts_remove,
            team_normas_get=monitor.handle_team_normas_get,
            team_normas_upload=monitor.handle_team_normas_upload,
            team_normas_delete=monitor.handle_team_normas_delete,
            team_normas_download=monitor.handle_team_normas_download,
            team_normas_from_file=monitor.handle_team_normas_from_file,
            team_card_from_file=monitor.handle_team_card_from_file,
            team_normas_from_whatsapp=monitor.handle_team_normas_from_whatsapp,
            team_normas_whatsapp_preview=monitor.handle_team_normas_whatsapp_preview,
            team_normas_preview=monitor.handle_team_normas_preview,
            team_legal_evaluations_list=monitor.handle_team_legal_evaluations_list,
            team_legal_evaluations_search=monitor.handle_team_legal_evaluations_search,
            team_legal_evaluation_get=monitor.handle_team_legal_evaluation_get,
            team_legal_evaluation_save=monitor.handle_team_legal_evaluation_save,
            team_articles_folder_upload=monitor.handle_team_articles_folder_upload,
            team_kanban_get=monitor.handle_team_kanban_get,
            team_kanban_save=monitor.handle_team_kanban_save,
            team_swarm_run=monitor.handle_team_swarm_run,
            team_context_get=monitor.handle_team_context_get,
            agent_action=monitor.handle_agent_action,
            hermes_dashboard_url=hermes_dashboard_url,
            dashboard_render_config={
                **cfg.dashboard_render.model_dump(),
                **cfg.swarm_robots.model_dump(),
            },
            ruflo_user_checkin_interval_seconds=(
                cfg.ruflo_chat.user_checkin_interval_seconds
            ),
            hermes_agent_create=(
                monitor.handle_hermes_agent_create
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            openai_swarm_create=(
                monitor.handle_openai_swarm_create
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            openai_swarm_list=(
                monitor.handle_openai_swarm_list
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_presets=(
                monitor.handle_swarm_presets
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_graph_run=(
                monitor.handle_swarm_graph_run
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_graph_preview=(
                monitor.handle_swarm_graph_preview
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_graph_resume=(
                monitor.handle_swarm_graph_resume
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_graph_overview=(
                monitor.handle_swarm_graph_overview
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_executions=(
                monitor.handle_swarm_executions
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_robots_snapshot=(
                monitor.handle_swarm_robots_snapshot
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_robots_snapshot_stream=(
                monitor.iter_swarm_robots_snapshot_sse
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_activity_changes=(
                monitor.handle_swarm_activity_changes
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_agent_history=(
                monitor.handle_swarm_agent_history
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_skills_list=(
                monitor.handle_swarm_skills_list
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_teams_board=(
                monitor.handle_swarm_teams_board
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_teams_claim=(
                monitor.handle_swarm_teams_claim
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_robot_create=(
                monitor.handle_swarm_robot_create
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_robot_get=(
                monitor.handle_swarm_robot_get
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_robot_source=(
                monitor.handle_swarm_robot_source
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_robot_update=(
                monitor.handle_swarm_robot_update
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_robot_delete=(
                monitor.handle_swarm_robot_delete
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_execution_cancel=(
                monitor.handle_swarm_execution_cancel
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_create=(
                monitor.handle_swarm_create
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_update=(
                monitor.handle_swarm_update
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_delete=(
                monitor.handle_swarm_delete
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_health=(
                monitor.handle_swarm_health
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_health_fix=(
                monitor.handle_swarm_health_fix
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_schedule=(
                monitor.handle_swarm_schedule
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_model=(
                monitor.handle_swarm_model
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            hermes_profiles_list=(
                monitor.handle_hermes_profiles_list
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            hermes_profile_delete=(
                monitor.handle_hermes_profile_delete
                if cfg.hermes
                else None
            ),
            hermes_role_presets=(
                monitor.handle_hermes_role_presets
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            hermes_profile_generate_personality=(
                monitor.handle_hermes_profile_generate_personality
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            hermes_roles_seed=(
                monitor.handle_hermes_roles_seed
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            hermes_roles_seed_status=(
                monitor.handle_hermes_roles_seed_status
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            hermes_skills_list=(
                monitor.handle_hermes_skills_list
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            hermes_kanban_projects=monitor.handle_hermes_kanban_projects,
            hermes_kanban_get=monitor.handle_hermes_kanban_get,
            hermes_kanban_source=(
                monitor.handle_hermes_kanban_source
                if cfg.hermes_agent_create.enabled
                else None
            ),
            hermes_kanban_action=monitor.handle_hermes_kanban_action,
            hermes_kanban_schedule=(
                monitor.handle_hermes_kanban_schedule
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            hermes_kanban_schedule_status=(
                monitor.handle_hermes_kanban_schedule_status
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            hermes_kanban_pm_analysis=monitor.handle_hermes_kanban_pm_analysis,
            hermes_kanban_task_history=monitor.handle_hermes_kanban_task_history,
            hermes_kanban_requirements=(
                monitor.handle_hermes_kanban_requirements
                if cfg.hermes_agent_create.enabled
                else None
            ),
            hermes_kanban_attachment_upload=monitor.handle_hermes_kanban_attachment_upload,
            hermes_kanban_attachment_upload_binary=monitor.handle_hermes_kanban_attachment_upload_binary,
            hermes_kanban_attachment_get=monitor.handle_hermes_kanban_attachment_get,
            hermes_kanban_attachments_zip_get=monitor.handle_hermes_kanban_attachments_zip_get,
            hermes_kanban_attachment_delete=monitor.handle_hermes_kanban_attachment_delete,
            hermes_kanban_attachment_move=monitor.handle_hermes_kanban_attachment_move,
            hermes_kanban_attachment_copy=monitor.handle_hermes_kanban_attachment_copy,
            hermes_mascots_list=(
                monitor.handle_hermes_mascots_list
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            hermes_local_folders=(
                monitor.handle_hermes_local_folders
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            hermes_cron_list=(
                monitor.handle_hermes_cron_list if cfg.hermes else None
            ),
            hermes_cron_swarm_timeline=(
                monitor.handle_hermes_cron_swarm_timeline if cfg.hermes else None
            ),
            hermes_cron_catch_up=(
                monitor.handle_hermes_cron_catch_up if cfg.hermes else None
            ),
            hermes_cron_resume_all=(
                monitor.handle_hermes_cron_resume_all if cfg.hermes else None
            ),
            hermes_cron_force_remove=(
                monitor.handle_hermes_cron_force_remove if cfg.hermes else None
            ),
            hermes_cron_force_remove_all=(
                monitor.handle_hermes_cron_force_remove_all if cfg.hermes else None
            ),
            hermes_cron_get=(
                monitor.handle_hermes_cron_get if cfg.hermes else None
            ),
            hermes_cron_edit=(
                monitor.handle_hermes_cron_edit if cfg.hermes else None
            ),
            hermes_cron_action=(
                monitor.handle_hermes_cron_action if cfg.hermes else None
            ),
            hermes_cron_result=(
                monitor.handle_hermes_cron_result if cfg.hermes else None
            ),
            hermes_cron_source=(
                monitor.handle_hermes_cron_source if cfg.hermes else None
            ),
            hermes_cron_create=(
                monitor.handle_hermes_cron_create if cfg.hermes else None
            ),
            hermes_cron_preview=(
                monitor.handle_hermes_cron_preview if cfg.hermes else None
            ),
            knowledge_get=monitor.handle_knowledge_get,
            entity_db_get=monitor.handle_entity_db_get,
            knowledge_extract=monitor.handle_knowledge_extract,
            knowledge_delete_project=monitor.handle_knowledge_delete_project,
            knowledge_delete_session=monitor.handle_knowledge_delete_session,
            hermes_recovery_status=(
                monitor.handle_hermes_recovery_status if cfg.hermes else None
            ),
            hermes_recovery_action=(
                monitor.handle_hermes_recovery_action if cfg.hermes else None
            ),
            hermes_cron_diagnose=(
                monitor.handle_hermes_cron_diagnose if cfg.hermes else None
            ),
            hermes_cron_diagnose_action=(
                monitor.handle_hermes_cron_diagnose_action if cfg.hermes else None
            ),
            openclaw_sessions_list=(
                monitor.handle_openclaw_sessions_list
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_session_new=(
                monitor.handle_openclaw_session_new
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_session_delete=(
                monitor.handle_openclaw_session_delete
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_sessions_purge=(
                monitor.handle_openclaw_sessions_purge
                if cfg.openclaw_chat.enabled
                else None
            ),
            chat_assign_team=(
                monitor.handle_chat_assign_team
                if cfg.openclaw_chat.enabled
                else None
            ),
            chat_session_swarm=(
                monitor.handle_chat_session_swarm
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_messages_get=(
                monitor.handle_openclaw_messages_get
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_skills_list=(
                monitor.handle_openclaw_skills_list
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_integration_get=(
                monitor.handle_openclaw_integration_get
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_tools_list=(
                monitor.handle_openclaw_tools_list
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_skill_dirs_add=(
                monitor.handle_openclaw_skill_dirs_add
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_chat_failures=(
                monitor.handle_openclaw_chat_failures
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_models_list=(
                monitor.handle_openclaw_models_list
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_models_catalog=(
                monitor.handle_openclaw_models_catalog
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_models_sync=(
                monitor.handle_openclaw_models_sync
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_replicate_models=(
                monitor.handle_openclaw_replicate_models
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_image_models_catalog=(
                monitor.handle_openclaw_image_models_catalog
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_replicate_generate=(
                monitor.handle_openclaw_replicate_generate
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_chat_widgets_action=(
                monitor.handle_openclaw_chat_widgets_action
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_chat_epub_reader=(
                monitor.handle_openclaw_chat_epub_reader
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_chat_book_image=(
                monitor.handle_openclaw_chat_book_image
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_chat_course_image=(
                monitor.handle_openclaw_chat_course_image
                if cfg.openclaw_chat.enabled
                else None
            ),
            codex_auth_status=(
                monitor.handle_codex_auth_status
                if cfg.openclaw_chat.enabled
                else None
            ),
            codex_login_start=(
                monitor.handle_codex_login_start
                if cfg.openclaw_chat.enabled
                else None
            ),
            codex_login_wait=(
                monitor.handle_codex_login_wait
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_connection_get=(
                monitor.handle_openclaw_connection_get
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_connection_set=(
                monitor.handle_openclaw_connection_set
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_fastpath_get=(
                monitor.handle_openclaw_fastpath_get
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_fastpath_set=(
                monitor.handle_openclaw_fastpath_set
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_token_mode_get=(
                monitor.handle_openclaw_token_mode_get
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_token_mode_set=(
                monitor.handle_openclaw_token_mode_set
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_tool_limit_get=(
                monitor.handle_openclaw_tool_limit_get
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_tool_limit_set=(
                monitor.handle_openclaw_tool_limit_set
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_tools_cap_get=(
                monitor.handle_openclaw_tools_cap_get
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_tools_cap_set=(
                monitor.handle_openclaw_tools_cap_set
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_attachment_upload=(
                monitor.handle_openclaw_attachment_upload
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_visual_memory_from_photo=(
                monitor.handle_openclaw_visual_memory_from_photo
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_profile_from_photo=(
                monitor.handle_openclaw_profile_from_photo
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_visual_memory_profiles=(
                monitor.handle_openclaw_visual_memory_profiles
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_visual_memory_preview=(
                monitor.handle_openclaw_visual_memory_preview
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_generated_images_list=(
                monitor.handle_openclaw_generated_images_list
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_generated_images_delete=(
                monitor.handle_openclaw_generated_images_delete
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_generated_images_register=(
                monitor.handle_openclaw_generated_images_register
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_minicurriculum_html=(
                monitor.handle_openclaw_minicurriculum_html
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_context_documents=(
                monitor.handle_openclaw_context_documents
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_session_context_get=(
                monitor.handle_openclaw_session_context_get
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_artifact_download=(
                monitor.handle_openclaw_artifact_download
                if cfg.openclaw_chat.enabled
                else None
            ),
            chat_tts=(
                monitor.handle_chat_tts
                if cfg.openclaw_chat.enabled
                else None
            ),
            chat_stt=(
                monitor.handle_chat_stt
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_send=(
                monitor.handle_openclaw_send
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_send_status=(
                monitor.handle_openclaw_send_status
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_send_cancel=(
                monitor.handle_openclaw_send_cancel
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_send_pending=(
                monitor.handle_openclaw_send_pending
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_send_queue=(
                monitor.handle_openclaw_send_queue
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_session_presence=(
                monitor.handle_openclaw_session_presence
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_tool_jobs=(
                monitor.handle_openclaw_tool_jobs
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_jobs_running=(
                monitor.handle_openclaw_jobs_running
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_jobs_cancel=(
                monitor.handle_openclaw_jobs_cancel
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_books_library=(
                monitor.handle_openclaw_books_library
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_articles_library=(
                monitor.handle_openclaw_articles_library
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_article_delete=(
                monitor.handle_openclaw_article_delete
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_book_structure=(
                monitor.handle_openclaw_book_structure
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_book_cover=(
                monitor.handle_openclaw_book_cover
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_book_epub=(
                monitor.handle_openclaw_book_epub
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_book_delete=(
                monitor.handle_openclaw_book_delete
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_courses_library=(
                monitor.handle_openclaw_courses_library
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_course_structure=(
                monitor.handle_openclaw_course_structure
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_course_html_site=(
                monitor.handle_openclaw_course_html_site
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_course_delete=(
                monitor.handle_openclaw_course_delete
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_sagas_library=(
                monitor.handle_openclaw_sagas_library
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_saga_structure=(
                monitor.handle_openclaw_saga_structure
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_saga_delete=(
                monitor.handle_openclaw_saga_delete
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_story_delete=(
                monitor.handle_openclaw_story_delete
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_page_delete=(
                monitor.handle_openclaw_page_delete
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_magazine_delete=(
                monitor.handle_openclaw_magazine_delete
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_scripts_library=(
                monitor.handle_openclaw_scripts_library
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_user_profile_dock=(
                monitor.handle_openclaw_user_profile_dock
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_user_profile_dock_generate=(
                monitor.handle_openclaw_user_profile_dock_generate
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_user_profile_dock_remove_photo=(
                monitor.handle_openclaw_user_profile_dock_remove_photo
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_script_structure=(
                monitor.handle_openclaw_script_structure
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_script_delete=(
                monitor.handle_openclaw_script_delete
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_script_act_delete=(
                monitor.handle_openclaw_script_act_delete
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_script_act_image=(
                monitor.handle_openclaw_script_act_image
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_gallery_image_route=(
                monitor.handle_openclaw_gallery_image_route
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_memory_list=(
                monitor.handle_openclaw_memory_list
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_project_memory_get=(
                monitor.handle_openclaw_project_memory_get
                if cfg.openclaw_chat.enabled
                else None
            ),
            openclaw_project_memory_put=(
                monitor.handle_openclaw_project_memory_put
                if cfg.openclaw_chat.enabled
                else None
            ),
            active_agents_snapshot=monitor.handle_active_agents_snapshot,
            ruflo_status=(
                monitor.handle_ruflo_status
                if cfg.ruflo_chat.enabled and cfg.openclaw_chat.enabled
                else None
            ),
            ruflo_sessions_list=(
                monitor.handle_ruflo_sessions_list
                if cfg.ruflo_chat.enabled and cfg.openclaw_chat.enabled
                else None
            ),
            ruflo_session_new=(
                monitor.handle_ruflo_session_new
                if cfg.ruflo_chat.enabled and cfg.openclaw_chat.enabled
                else None
            ),
            ruflo_session_delete=(
                monitor.handle_ruflo_session_delete
                if cfg.ruflo_chat.enabled and cfg.openclaw_chat.enabled
                else None
            ),
            ruflo_messages_get=(
                monitor.handle_ruflo_messages_get
                if cfg.ruflo_chat.enabled and cfg.openclaw_chat.enabled
                else None
            ),
            ruflo_route_preview=(
                monitor.handle_ruflo_route_preview
                if cfg.ruflo_chat.enabled and cfg.openclaw_chat.enabled
                else None
            ),
            ruflo_send=(
                monitor.handle_ruflo_send
                if cfg.ruflo_chat.enabled and cfg.openclaw_chat.enabled
                else None
            ),
            ruflo_motores_status=(
                monitor.handle_ruflo_motores_status
                if cfg.ruflo_chat.enabled and cfg.openclaw_chat.enabled
                else None
            ),
            ruflo_motores_start=(
                monitor.handle_ruflo_motores_start
                if cfg.ruflo_chat.enabled and cfg.openclaw_chat.enabled
                else None
            ),
            ruflo_motores_stop=(
                monitor.handle_ruflo_motores_stop
                if cfg.ruflo_chat.enabled and cfg.openclaw_chat.enabled
                else None
            ),
            ruflo_motores_rebuild=(
                monitor.handle_ruflo_motores_rebuild
                if cfg.ruflo_chat.enabled and cfg.openclaw_chat.enabled
                else None
            ),
            errors_list=(
                monitor.handle_errors_list
                if cfg.error_log.enabled
                else None
            ),
            cron_concurrency_status=(
                monitor.handle_cron_concurrency_status
                if cfg.cron_concurrency.enabled
                else None
            ),
            cron_concurrency_release=(
                monitor.handle_cron_concurrency_release
                if cfg.cron_concurrency.enabled
                else None
            ),
            model_quotas_get=monitor.handle_model_quotas_get,
            model_quotas_update=monitor.handle_model_quotas_update,
            env_keys_get=monitor.handle_env_keys_get,
            env_keys_update=monitor.handle_env_keys_update,
            env_keys_import=monitor.handle_env_keys_import,
            env_keys_import_roteiro_viral=monitor.handle_env_keys_import_roteiro_viral,
            env_keys_prune=monitor.handle_env_keys_prune_env,
            wacli_auth_status=monitor.handle_wacli_auth_status,
            wacli_auth_qr=monitor.handle_wacli_auth_qr,
            wacli_auth_wait=monitor.handle_wacli_auth_wait,
            wacli_unlock=monitor.handle_wacli_unlock,
            whatsapp_send=(
                monitor.handle_whatsapp_send_http
                if cfg.whatsapp_digest.recipient
                else None
            ),
            whatsapp_queue=(
                monitor.handle_whatsapp_queue_status
                if cfg.whatsapp_digest.recipient
                else None
            ),
            whatsapp_inbound=(
                monitor.handle_whatsapp_inbound
                if cfg.whatsapp_digest.inbound_enabled
                else None
            ),
            whatsapp_status=(
                monitor.handle_whatsapp_status
                if cfg.whatsapp_digest.recipient
                else None
            ),
            team_whatsapp_numbers_get=monitor.handle_team_whatsapp_numbers_get,
            team_whatsapp_numbers_set=monitor.handle_team_whatsapp_numbers_set,
            team_whatsapp_number_add=monitor.handle_team_whatsapp_number_add,
            team_whatsapp_number_remove=monitor.handle_team_whatsapp_number_remove,
            team_whatsapp_send=monitor.handle_team_whatsapp_send,
            team_whatsapp_broadcast=monitor.handle_team_whatsapp_broadcast,
            team_whatsapp_send_to_group=monitor.handle_team_whatsapp_send_to_group,
            team_group_messages_get=monitor.handle_team_group_messages_get,
            team_email_messages_get=monitor.handle_team_email_messages_get,
            team_email_sync=monitor.handle_team_email_sync,
            team_email_send=monitor.handle_team_email_send,
            skill_suggestions_list=(
                monitor.handle_skill_suggestions_list
                if cfg.skill_discovery.enabled
                else None
            ),
            skill_suggestions_scan=(
                monitor.handle_skill_suggestions_scan
                if cfg.skill_discovery.enabled
                else None
            ),
            skill_suggestions_dismiss=(
                monitor.handle_skill_suggestions_dismiss
                if cfg.skill_discovery.enabled
                else None
            ),
            mcp_skills=monitor.handle_mcp_skills,
            mcp_cards=monitor.handle_mcp_cards,
            whatsapp_allowlist_list=monitor.handle_whatsapp_allowlist_list,
            whatsapp_allowlist_add=monitor.handle_whatsapp_allowlist_add,
            whatsapp_allowlist_update=monitor.handle_whatsapp_allowlist_update,
            whatsapp_allowlist_remove=monitor.handle_whatsapp_allowlist_remove,
            whatsapp_allowlist_toggle=monitor.handle_whatsapp_allowlist_toggle,
            whatsapp_allowlist_contacts=monitor.handle_whatsapp_allowlist_contacts,
            whatsapp_agenda_list=monitor.handle_whatsapp_agenda_list,
            whatsapp_agenda_sync=monitor.handle_whatsapp_agenda_sync,
            whatsapp_agenda_stats=monitor.handle_whatsapp_agenda_stats,
            whatsapp_messages_search=monitor.handle_whatsapp_messages_search,
            whatsapp_norma_candidates=monitor.handle_whatsapp_norma_candidates,
            latex_workspaces_list=monitor.handle_latex_workspaces_list,
            latex_workspace_register=monitor.handle_latex_workspace_register,
            latex_workspace_remove=monitor.handle_latex_workspace_remove,
            latex_articles_list=monitor.handle_latex_articles_list,
            latex_files_list=monitor.handle_latex_files_list,
            latex_tex_files_list=monitor.handle_latex_tex_files_list,
            latex_bib_files_list=monitor.handle_latex_bib_files_list,
            latex_file_delete=monitor.handle_latex_file_delete,
            latex_file_upload=monitor.handle_latex_file_upload,
            latex_file_get=monitor.handle_latex_file_get,
            latex_file_save=monitor.handle_latex_file_save,
            latex_improve_ops=monitor.handle_latex_improve_ops,
            latex_tex_improve=monitor.handle_latex_tex_improve,
            latex_compile=monitor.handle_latex_compile,
            latex_tex_get=monitor.handle_latex_tex_get,
            latex_tex_save=monitor.handle_latex_tex_save,
            latex_tex_version_save=monitor.handle_latex_tex_version_save,
            latex_tex_version_list=monitor.handle_latex_tex_version_list,
            latex_tex_version_get=monitor.handle_latex_tex_version_get,
            latex_tex_version_restore=monitor.handle_latex_tex_version_restore,
            latex_objectives_md=monitor.handle_latex_objectives_md,
            latex_tex_chat=monitor.handle_latex_tex_chat,
            latex_objectives_suggest=monitor.handle_latex_objectives_suggest,
            latex_objectives_get=monitor.handle_latex_objectives_get,
            latex_objectives_save=monitor.handle_latex_objectives_save,
            attendance_get=monitor.handle_attendance_get,
            attendance_save=monitor.handle_attendance_save,
            latex_tex_apply_template=monitor.handle_latex_tex_apply_template,
            latex_tex_restore_template=monitor.handle_latex_tex_restore_template,
            latex_pdf_path=monitor.handle_latex_pdf_path,
            latex_figure_path=monitor.handle_latex_figure_path,
            latex_quality_list=monitor.handle_latex_quality_list,
            latex_quality_detail=monitor.handle_latex_quality_detail,
            latex_improvements_list=monitor.handle_latex_improvements_list,
            latex_improvements_history=monitor.handle_latex_improvements_history,
            latex_improvements_resolve=monitor.handle_latex_improvements_resolve,
            latex_improvements_evaluate=monitor.handle_latex_improvements_evaluate,
            latex_templates_list=monitor.handle_latex_templates_list,
            latex_template_upload=monitor.handle_latex_template_upload,
            latex_template_delete=monitor.handle_latex_template_delete,
            latex_template_apply=monitor.handle_latex_template_apply,
            latex_template_get=monitor.handle_latex_template_get,
            latex_template_preview=monitor.handle_latex_template_preview,
            alerting_clear=monitor.handle_alerting_clear,
            priority_queue_get=(
                monitor.handle_priority_queue_get
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
            priority_queue_post=(
                monitor.handle_priority_queue_post
                if cfg.hermes and cfg.hermes_agent_create.enabled
                else None
            ),
        )

    try:
        asyncio.run(monitor.run())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)


if __name__ == "__main__":
    main()
