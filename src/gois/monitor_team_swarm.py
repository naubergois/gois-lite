"""Team-linked swarm execution helpers for GoisMonitor."""

from __future__ import annotations

import logging
from typing import Any, Optional

from .accounts import TeamRecord, UserRecord

log = logging.getLogger(__name__)

class MonitorTeamSwarmMixin:
    def _maybe_autopush_team(self, team: TeamRecord, result: Any) -> None:
        """Auto-push the team's GitHub repos when an activity completes ok.

        No-op unless the team opted in (``github_autopush``) and the run
        succeeded. Failures are attached to ``result['github_autopush']`` and
        never interrupt the swarm flow.
        """
        if not isinstance(result, dict) or not result.get("ok"):
            return
        try:
            from .team_git import autopush_on_activity

            summary = autopush_on_activity(
                [team.to_public()],
                team.id,
                commit_message=f"chore: auto-push após atividade do time {team.name}",
                trigger="team_swarm_run",
            )
        except Exception as exc:  # noqa: BLE001 - boundary, must not break run
            log.warning("auto-push do time %s falhou: %s", team.id, exc)
            return
        if summary is not None:
            result["github_autopush"] = summary

    def _sync_team_profiles_to_swarm(self, team: TeamRecord) -> None:
        """Merge team profile slugs into the linked swarm definition."""
        swarm_name = str(team.swarm_name or "").strip()
        if not swarm_name or not team.profile_slugs:
            return
        from .openai_swarm import update_swarm_definition

        try:
            update_swarm_definition(
                swarm_name,
                hermes_profiles=list(dict.fromkeys(team.profile_slugs)),
            )
        except Exception as exc:
            log.debug("team swarm sync skipped for %s: %s", swarm_name, exc)

    def _team_swarm_objective(
        self,
        team: TeamRecord,
        board: dict[str, Any],
        override: Optional[str] = None,
    ) -> str:
        text = str(override or "").strip()
        if text:
            return text
        active_cols = {"todo", "doing", "backlog", "review", "testes-usabilidade"}
        lines: list[str] = []
        for task in board.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            col = str(task.get("column") or "").strip().lower()
            if col not in active_cols:
                continue
            tid = str(task.get("id") or "").strip()
            title = str(task.get("title") or tid).strip()
            if not title:
                continue
            prefix = f"{tid}: " if tid else ""
            lines.append(f"- {prefix}{title}")
        if lines:
            header = f"Executar o trabalho pendente do time {team.name} no kanban"
            return header + ":\n" + "\n".join(lines[:12])
        desc = str(team.description or "").strip()
        if desc:
            return f"Executar objetivos do time {team.name}: {desc}"
        return f"Executar o swarm do time {team.name}"

    def _ephemeral_team_swarm_state(self, team: TeamRecord, swarm_name: str) -> dict[str, Any]:
        from .openai_swarm import ensure_swarm_handoffs
        from .swarm_robots import _filter_live_profile_slugs, _guess_orchestrator_slug

        profiles = _filter_live_profile_slugs(team.profile_slugs or [])
        state: dict[str, Any] = {
            "name": swarm_name,
            "description": str(team.description or team.name or swarm_name),
            "topology": "team",
            "entry_agent": "",
            "hermes_profiles": profiles,
            "agents": [],
        }
        entry = _guess_orchestrator_slug(profiles, [], None)
        if entry and entry in profiles:
            state["entry_agent"] = entry
        return ensure_swarm_handoffs(state)

    def _team_swarm_profiles(self, team: TeamRecord, swarm_name: str) -> list[str]:
        """Profile slugs that make up the team's swarm (team roles + swarm file)."""
        from .swarm_robots import _filter_live_profile_slugs

        slugs: list[str] = []
        for s in _filter_live_profile_slugs(team.profile_slugs or []):
            if s not in slugs:
                slugs.append(s)
        try:
            from .swarm_graph import _load_swarm_state

            state = _load_swarm_state(swarm_name)
        except Exception:  # noqa: BLE001 - best-effort enrichment
            state = None
        if isinstance(state, dict):
            for s in _filter_live_profile_slugs(state.get("hermes_profiles") or []):
                if s not in slugs:
                    slugs.append(s)
        return slugs

    def _team_swarm_busy(self, team: TeamRecord, swarm_name: str) -> dict[str, Any]:
        """Report whether the team's swarm is already running work.

        A swarm is "busy" when any of its profile slugs has a live job (cron,
        kanban schedule, chat, tool run, or priority-queue card). Returns
        ``{"busy": bool, "running": [...]}`` and never raises — a detection
        failure degrades to "not busy" so the run is never wrongly blocked.
        """
        slugs = self._team_swarm_profiles(team, swarm_name)
        if not slugs:
            return {"busy": False, "running": []}
        try:
            from .swarm_robots import _running_by_profile

            running_map = _running_by_profile(self)
        except Exception as exc:  # noqa: BLE001 - degrade to "not busy"
            log.debug("team swarm busy check failed for %s: %s", swarm_name, exc)
            return {"busy": False, "running": []}

        running: list[dict[str, Any]] = []
        wanted = set(slugs)
        for slug, jobs in (running_map or {}).items():
            if slug not in wanted:
                continue
            for job in jobs or []:
                if not isinstance(job, dict):
                    continue
                running.append(
                    {
                        "slug": slug,
                        "kind": job.get("kind"),
                        "name": job.get("name"),
                        "job_id": job.get("job_id"),
                    }
                )
        return {"busy": bool(running), "running": running}

    def _run_team_swarm_ruflo_engine(
        self,
        *,
        team: TeamRecord,
        swarm_name: str,
        objective: str,
        run_payload: dict[str, Any],
        swarm_state: Optional[dict[str, Any]],
        user: Optional[UserRecord],
        actor: Any,
    ) -> dict[str, Any]:
        from .ruflo_swarm_engine import build_ruflo_swarm_engine
        from .ruflo_swarm_hooks import build_ruflo_swarm_hooks, swarm_run_light_mode

        engine_cfg = getattr(self.cfg, "swarm_ruflo_engine", None)
        engine = build_ruflo_swarm_engine(engine_cfg)
        if engine is None:
            return {"ok": False, "error": "ruflo engine not configured", "engine": "ruflo"}

        profiles = self._team_swarm_profiles(team, swarm_name)
        if not profiles and isinstance(swarm_state, dict):
            from .swarm_graph import compile_swarm_graph

            graph = compile_swarm_graph(swarm_state)
            profiles = list(graph.order)

        team_ctx: dict[str, Any] = {}
        run_all = bool(run_payload.get("run_all", True))
        use_team_cards = bool(run_payload.get("use_team_cards", True))
        team_id = str(run_payload.get("team_id") or team.id).strip()
        if run_all and team_id and use_team_cards:
            from .swarm_robots import prepare_swarm_test_team_context

            max_cards = run_payload.get("max_cards")
            if max_cards is None:
                max_cards = 1
            else:
                try:
                    max_cards = int(max_cards)
                except (TypeError, ValueError):
                    max_cards = 1

            team_ctx = prepare_swarm_test_team_context(
                self, team_id, swarm_name, actor, max_cards=max_cards
            )
            if team_ctx.get("ok"):
                team_objective = str(team_ctx.get("objective") or "").strip()
                if team_objective:
                    objective = team_objective

        progress_job_id = str(run_payload.get("progress_job_id") or "").strip() or None
        self._announce_swarm_run_progress(
            progress_job_id,
            swarm_name=swarm_name,
            team_ctx=team_ctx if isinstance(team_ctx, dict) else {},
        )

        hooks_cfg = getattr(self.cfg, "swarm_ruflo_hooks", None)
        hooks = build_ruflo_swarm_hooks(
            hooks_cfg, light=swarm_run_light_mode(self.cfg, run_payload)
        )

        from .swarm_memory import build_swarm_memory
        from .swarm_graph import build_llm_node_runner, handoff_context_from_team_ctx
        from .kanban_ide_handoff_ops import _monitor_base_url

        chat_cfg = self.cfg.openclaw_chat if self.cfg.openclaw_chat.enabled else None
        handoff_ctx = handoff_context_from_team_ctx(
            team_ctx if isinstance(team_ctx, dict) else None,
            base_url=_monitor_base_url(),
        )
        open_ide = True
        if isinstance(run_payload, dict) and "open_ide" in run_payload:
            open_ide = bool(run_payload.get("open_ide", True))
        runner = build_llm_node_runner(
            chat_cfg=chat_cfg,
            agent_cfg=self.cfg.agent,
            swarm_name=swarm_name,
            handoff_ctx=handoff_ctx,
            open_ide=open_ide,
        )
        memory = build_swarm_memory(self.cfg.swarm_memory)
        agent_cards_map: dict[str, list[dict[str, Any]]] = {}
        if team_ctx.get("ok"):
            for card in team_ctx.get("cards") or []:
                if not isinstance(card, dict):
                    continue
                prof = str(card.get("profile") or card.get("agent") or "").strip()
                if prof:
                    agent_cards_map.setdefault(prof, []).append(card)

        result = engine.run(
            swarm_name=swarm_name,
            objective=objective,
            profiles=profiles,
            hooks=hooks,
            node_runner=runner,
            memory=memory,
            memory_top_k=self.cfg.swarm_memory.retrieval_top_k,
            agent_cards=agent_cards_map or None,
            hitl_enabled=bool(
                self.cfg.swarm_routing.hitl_enabled
                or getattr(engine_cfg, "hitl_enabled", False)
            ),
            swarm_state=swarm_state if isinstance(swarm_state, dict) else None,
            a2a_cfg=getattr(self.cfg, "swarm_ruflo_a2a", None),
        )

        if isinstance(result, dict) and result.get("ok"):
            self._invalidate_swarm_robots_cache()
            if team_ctx.get("ok") and run_all:
                from .swarm_robots import finalize_swarm_kanban_cards

                completed_ids = finalize_swarm_kanban_cards(
                    self,
                    team_ctx=team_ctx,
                    run_result=result,
                )
                if completed_ids:
                    result["kanban_completed"] = completed_ids
                result["cards_count"] = team_ctx.get("cards_count", 0)

        return result

    def handle_team_swarm_run(
        self, team_id: str, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Execute the team's swarm against its kanban cards.

        Runs every agent once (``run_all``) with per-agent card context
        (``use_team_cards``): open cards are auto-assigned, moved to doing,
        executed, and finalized to done when outputs report completion.
        """
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self.accounts.get_team(team_id, actor.id)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        swarm_name = str(team.swarm_name or "").strip() or team.id

        # Don't pile a second run on top of work already in flight unless the
        # caller explicitly forces it. Keeps a team's swarm from duplicating
        # effort on the same kanban cards.
        force = bool(payload.get("force") or payload.get("force_run"))
        busy = self._team_swarm_busy(team, swarm_name)
        if busy.get("busy") and not force:
            running = busy.get("running") or []
            return {
                "ok": False,
                "busy": True,
                "team_id": team.id,
                "team_name": team.name,
                "swarm_name": swarm_name,
                "running": running,
                "error": (
                    f"O swarm do time '{team.name}' está ocupado "
                    f"({len(running)} job(s) em execução). "
                    "Aguarde concluir ou reexecute com force=true."
                ),
            }

        board = self.accounts.read_kanban(team.id, actor.id)
        objective = self._team_swarm_objective(
            team,
            board if isinstance(board, dict) else {},
            str(payload.get("objective") or payload.get("text") or ""),
        )

        from .swarm_graph import _load_swarm_state

        run_payload: dict[str, Any] = {
            "swarm_name": swarm_name,
            "objective": objective,
            "team_id": team.id,
            "run_all": bool(payload.get("run_all", True)),
            "use_team_cards": bool(payload.get("use_team_cards", True)),
            "max_cards": payload.get("max_cards", 1),
        }
        for key in ("conditional_handoff", "pause_before", "use_langgraph", "progress_job_id"):
            if key in payload:
                run_payload[key] = payload[key]

        swarm_state = _load_swarm_state(swarm_name)
        if swarm_state is None:
            from .openai_swarm import find_swarm_name_for_team

            alt = find_swarm_name_for_team(team.id, hint_name=swarm_name)
            if alt:
                swarm_name = alt
                swarm_state = _load_swarm_state(swarm_name)
                if swarm_state is not None and not str(team.swarm_name or "").strip():
                    profiles = list(
                        dict.fromkeys(
                            [str(p).strip() for p in (team.profile_slugs or []) if str(p).strip()]
                            + [
                                str(p).strip()
                                for p in (swarm_state.get("hermes_profiles") or [])
                                if str(p).strip()
                            ]
                        )
                    )
                    self._link_team_to_swarm(
                        team,
                        swarm_name,
                        actor,
                        profile_slugs=profiles or None,
                    )
                    team = self.accounts.get_team(team.id, actor.id)
                    run_payload["swarm_name"] = swarm_name
        if swarm_state is None:
            if not team.profile_slugs:
                return {
                    "ok": False,
                    "error": (
                        f"swarm '{swarm_name}' não encontrado e o time não tem papéis "
                        "para montar um swarm efêmero"
                    ),
                }
            swarm_state = self._ephemeral_team_swarm_state(team, swarm_name)
            run_payload["state"] = swarm_state

        from .swarm_robots import augment_development_swarm_objective

        objective = augment_development_swarm_objective(
            objective,
            monitor=self,
            team=team,
            swarm_name=swarm_name,
            swarm_state=swarm_state if isinstance(swarm_state, dict) else None,
        )
        run_payload["objective"] = objective

        coord_plan: Optional[dict[str, Any]] = None
        engine_cfg = getattr(self.cfg, "swarm_ruflo_engine", None)
        from .ruflo_swarm_engine import build_ruflo_swarm_engine, use_ruflo_engine

        if use_ruflo_engine(
            engine_cfg, payload_engine=str(payload.get("engine") or "")
        ):
            ruflo_result = self._run_team_swarm_ruflo_engine(
                team=team,
                swarm_name=swarm_name,
                objective=objective,
                run_payload=run_payload,
                swarm_state=swarm_state,
                user=user,
                actor=actor,
            )
            if isinstance(ruflo_result, dict):
                ruflo_result.setdefault("team_id", team.id)
                ruflo_result.setdefault("team_name", team.name)
                ruflo_result.setdefault("swarm_name", swarm_name)
                ruflo_result.setdefault("objective", objective)
            if (
                isinstance(ruflo_result, dict)
                and ruflo_result.get("ok")
            ) or not (
                engine_cfg is not None
                and getattr(engine_cfg, "fallback_to_hermes", True)
            ):
                self._maybe_autopush_team(team, ruflo_result)
                return ruflo_result
            if isinstance(ruflo_result, dict):
                log.warning(
                    "team swarm: ruflo engine failed (%s); falling back to hermes",
                    ruflo_result.get("error"),
                )
                ruflo_result["ruflo_fallback"] = True

        coord_cfg = getattr(self.cfg, "swarm_ruflo_coordinator", None)
        if coord_cfg is not None and getattr(coord_cfg, "enabled", False):
            from .ruflo_swarm_coordinator import build_ruflo_coordinator

            coordinator = build_ruflo_coordinator(coord_cfg)
            if coordinator is not None:
                available = self._team_swarm_profiles(team, swarm_name)
                if not available and isinstance(swarm_state, dict):
                    from .swarm_graph import compile_swarm_graph

                    graph = compile_swarm_graph(swarm_state)
                    available = list(graph.order)
                coord_plan = coordinator.coordinate_team_swarm(
                    team_id=team.id,
                    team_name=team.name,
                    swarm_name=swarm_name,
                    objective=objective,
                    available_agents=available,
                )
                if coord_plan.get("agent_order"):
                    run_payload["order_override"] = coord_plan["agent_order"]

        result = self.handle_swarm_graph_run(run_payload, user)
        if isinstance(result, dict):
            result.setdefault("team_id", team.id)
            result.setdefault("team_name", team.name)
            result.setdefault("swarm_name", swarm_name)
            result.setdefault("objective", objective)
            if coord_plan is not None:
                result["ruflo_coordinator"] = coord_plan
                if coord_plan.get("agent_order"):
                    result["order"] = coord_plan["agent_order"]
        self._maybe_autopush_team(team, result)
        return result
