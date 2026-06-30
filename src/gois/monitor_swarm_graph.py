"""Swarm graph execution and preset listing."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .accounts import UserRecord

log = logging.getLogger(__name__)


class MonitorSwarmGraphMixin:
    def _resolve_swarm_run_workdir(
        self,
        payload: dict,
        team_ctx: dict[str, Any],
        user: Optional[UserRecord],
        *,
        team_id: str = "",
    ) -> str:
        wd = str(payload.get("workdir") or "").strip()
        if wd:
            return wd
        if isinstance(team_ctx, dict):
            wd = str(team_ctx.get("workdir") or "").strip()
            if wd:
                return wd
        tid = str(team_id or payload.get("team_id") or "").strip()
        if not tid and isinstance(team_ctx, dict):
            tid = str(team_ctx.get("team_id") or "").strip()
        actor = self._accounts_actor(user)
        if tid and actor is not None:
            try:
                team = self.accounts.get_team(tid, actor.id)
                return str(self.accounts.team_workdir(team))
            except ValueError:
                pass
        from .local_paths import project_stack_root

        return str(project_stack_root())

    def _build_swarm_llm_runner(
        self,
        *,
        swarm_name: str,
        team_ctx: dict[str, Any] | None = None,
        checkpoint: dict[str, Any] | None = None,
        payload: dict | None = None,
        on_run_progress=None,
        model_override=None,
    ):
        from .kanban_ide_handoff_ops import _monitor_base_url
        from .swarm_graph import (
            build_llm_node_runner,
            handoff_context_from_checkpoint,
            handoff_context_from_team_ctx,
        )

        chat_cfg = self.cfg.openclaw_chat if self.cfg.openclaw_chat.enabled else None
        base_url = _monitor_base_url()
        handoff_ctx = handoff_context_from_team_ctx(
            team_ctx if isinstance(team_ctx, dict) else None,
            base_url=base_url,
        )
        if handoff_ctx is None and isinstance(checkpoint, dict):
            handoff_ctx = handoff_context_from_checkpoint(checkpoint, base_url=base_url)
        open_ide = True
        if isinstance(payload, dict) and "open_ide" in payload:
            open_ide = bool(payload.get("open_ide", True))
        return build_llm_node_runner(
            chat_cfg=chat_cfg,
            agent_cfg=self.cfg.agent,
            swarm_name=swarm_name,
            model_override=model_override,
            on_run_progress=on_run_progress,
            handoff_ctx=handoff_ctx,
            open_ide=open_ide,
        )

    def _start_swarm_execution_record(
        self,
        *,
        swarm_name: str,
        objective: str,
        payload: dict,
        team_ctx: dict[str, Any],
        user: Optional[UserRecord],
        team_id: str = "",
        engine: str = "",
        resume: bool = False,
    ) -> Optional[dict[str, Any]]:
        runs = getattr(self, "_swarm_exec_runs", None)
        if runs is None:
            runs = {}
            self._swarm_exec_runs = runs
        if resume:
            existing = runs.get(swarm_name)
            if isinstance(existing, dict) and existing.get("execution_id"):
                return existing
        try:
            from . import swarm_execution_history as history

            workdir = self._resolve_swarm_run_workdir(
                payload, team_ctx, user, team_id=team_id
            )
            baseline = history.git_baseline(workdir)
            execution_id = history.record_execution_start(
                swarm_name=swarm_name,
                objective=objective,
                team_id=team_id or str(payload.get("team_id") or "").strip(),
                workdir=workdir,
                engine=engine,
                source=str(payload.get("source") or "graph_run").strip(),
                baseline=baseline,
            )
            if not execution_id:
                return None
            ctx = {
                "execution_id": execution_id,
                "swarm_name": swarm_name,
                "workdir": workdir,
                "baseline": baseline,
            }
            runs[swarm_name] = ctx
            return ctx
        except Exception as exc:  # noqa: BLE001
            log.debug("swarm exec history: start failed for %s: %s", swarm_name, exc)
            return None

    def _finish_swarm_execution_record(
        self, ctx: Optional[dict[str, Any]], result: dict[str, Any]
    ) -> None:
        if not ctx or not ctx.get("execution_id"):
            return
        swarm_name = str(ctx.get("swarm_name") or "").strip()
        try:
            from . import swarm_execution_history as history

            if result.get("paused"):
                history.record_execution_pause(ctx["execution_id"])
                return

            status = str(result.get("status") or ("done" if result.get("ok") else "error"))
            if result.get("rejected"):
                status = "rejected"
            files_changed = history.git_changed_files(
                ctx.get("workdir"), ctx.get("baseline")
            )
            summary = str(result.get("final") or "").strip()
            if not summary:
                state = result.get("state")
                if isinstance(state, dict):
                    hist = state.get("history") or []
                    if hist and isinstance(hist[-1], dict):
                        summary = str(hist[-1].get("output") or "").strip()
            history.record_execution_finish(
                ctx["execution_id"],
                status=status,
                files_changed=files_changed,
                summary=summary[:4000],
                error=str(result.get("error") or "")[:4000],
                result=result if isinstance(result, dict) else None,
            )
            runs = getattr(self, "_swarm_exec_runs", None)
            if isinstance(runs, dict) and swarm_name:
                runs.pop(swarm_name, None)
        except Exception as exc:  # noqa: BLE001
            log.debug("swarm exec history: finish failed for %s: %s", swarm_name, exc)

    def handle_swarm_executions(
        self,
        query: dict,
        user: Optional[UserRecord] = None,
    ) -> dict[str, Any]:
        """Return persisted swarm execution history (files changed, status, etc.)."""
        if self.cfg.auth.enabled and self._accounts_actor(user) is None:
            return {"ok": False, "error": "not authenticated"}
        swarm_name = str(query.get("swarm_name") or query.get("name") or "").strip()
        team_id = str(query.get("team_id") or "").strip()
        try:
            limit = max(1, min(200, int(str(query.get("limit") or "50"))))
        except ValueError:
            limit = 50
        try:
            from . import swarm_execution_history as history

            executions = history.list_swarm_executions(
                swarm_name=swarm_name or None,
                team_id=team_id or None,
                limit=limit,
            )
            return {
                "ok": True,
                "swarm_name": swarm_name or None,
                "team_id": team_id or None,
                "executions": executions,
            }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _swarm_node_progress_callback(
        self, progress_job_id: Optional[str]
    ) -> Optional[Any]:
        if not progress_job_id:
            return None
        from .chat_jobs import append_progress, set_block_progress

        def _cb(node_name: str, turn: int, total: int) -> None:
            label = f"Agente {node_name}" if node_name else "Swarm a iniciar…"
            msg = f"{label} ({turn}/{total})"
            set_block_progress(
                progress_job_id,
                turn,
                total,
                message=msg,
            )
            append_progress(progress_job_id, msg)

        return _cb

    def _swarm_run_progress_callback(
        self, progress_job_id: Optional[str]
    ) -> Optional[Any]:
        if not progress_job_id:
            return None
        from .chat_jobs import append_progress

        def _cb(message: str) -> None:
            line = str(message or "").strip()
            if line:
                append_progress(progress_job_id, line)

        return _cb

    def _announce_swarm_run_progress(
        self,
        progress_job_id: Optional[str],
        *,
        swarm_name: str,
        team_ctx: dict[str, Any],
    ) -> None:
        if not progress_job_id:
            return
        from .chat_jobs import append_progress

        if not team_ctx.get("ok"):
            return
        card_id = str(team_ctx.get("selected_card_id") or "").strip()
        card_title = ""
        for cards in (team_ctx.get("agent_cards") or {}).values():
            if not isinstance(cards, list) or not cards:
                continue
            row = cards[0]
            if isinstance(row, dict):
                card_title = str(row.get("title") or "").strip()
                if not card_id:
                    card_id = str(row.get("id") or "").strip()
                break
        if card_id or card_title:
            if card_id and card_title:
                append_progress(
                    progress_job_id,
                    f"Card a resolver: **{card_id}** — {card_title}",
                )
            else:
                append_progress(
                    progress_job_id,
                    f"Card a resolver: **{card_id or card_title}**",
                )

    def handle_swarm_graph_preview(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Preview which kanban card(s) a swarm run would pick (read-only)."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}

        swarm_name = str(payload.get("swarm_name") or payload.get("name") or "").strip()
        team_id = str(payload.get("team_id") or "").strip()
        if not swarm_name and team_id:
            try:
                team = self.accounts.get_team(team_id, actor.id)
                swarm_name = str(team.swarm_name or "").strip() or team.id
            except ValueError:
                pass
        if not swarm_name and not team_id:
            return {"ok": False, "error": "swarm_name or team_id is required"}
        if not team_id:
            from .swarm_graph import _load_swarm_state
            from .swarm_robots import _profile_team_map, _resolve_swarm_team_meta

            state_probe = _load_swarm_state(swarm_name)
            if isinstance(state_probe, dict):
                team_id = str(state_probe.get("team_id") or "").strip()
            if not team_id:
                team_map = _profile_team_map(self, actor)
                meta = _resolve_swarm_team_meta(
                    state_probe if isinstance(state_probe, dict) else {"name": swarm_name},
                    team_map,
                )
                team_id = str(meta.get("team_id") or "").strip()

        max_cards = payload.get("max_cards", 1)
        try:
            max_cards = int(max_cards)
        except (TypeError, ValueError):
            max_cards = 1

        from .swarm_robots import preview_swarm_run_cards

        if team_id:
            return preview_swarm_run_cards(
                self,
                team_id,
                swarm_name,
                actor,
                max_cards=max_cards,
            )
        return {
            "ok": True,
            "swarm_name": swarm_name,
            "team_id": "",
            "next_card": None,
            "next_cards": [],
            "pending_count": 0,
            "hint": "Swarm sem time vinculado — execução usa só o objetivo informado.",
        }

    def handle_swarm_presets(
        self,
        user: Optional[UserRecord] = None,
        query: Optional[dict[str, str]] = None,
    ) -> dict:
        """List ready-made swarm team templates."""
        if self.cfg.auth.enabled and self._accounts_actor(user) is None:
            return {"ok": False, "error": "not authenticated"}
        from .swarm_presets import list_swarm_presets

        q = (query or {}).get("q")
        category = (query or {}).get("category")
        raw_limit = (query or {}).get("limit")
        raw_offset = (query or {}).get("offset")
        limit = int(raw_limit) if raw_limit not in (None, "") else 120
        offset = int(raw_offset) if raw_offset not in (None, "") else 0
        return list_swarm_presets(q=q, category=category, limit=limit, offset=offset)

    def handle_openai_swarm_list(self) -> list:
        """List all created swarms."""
        from .openai_swarm import list_swarms

        return list_swarms()

    def handle_swarm_graph_run(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Execute a swarm as an executable graph (Phase 1).

        Compiles the swarm's ``handoff_to``/``entry_agent``/``topology`` into a
        real control flow (LangGraph when available, deterministic fallback
        otherwise) and runs each node through its own model + SOUL via the LLM
        gateway, checkpointing to ``.stack/swarms/<name>/graph_state.json``.
        """
        blocked = self._model_quota_guard()
        if blocked is not None:
            return blocked
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes or not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled in config"}

        swarm_name = str(payload.get("swarm_name") or payload.get("name") or "").strip()
        if not swarm_name:
            return {"ok": False, "error": "swarm_name is required"}
        objective = str(payload.get("objective") or payload.get("text") or "").strip()

        use_langgraph = payload.get("use_langgraph")
        if use_langgraph is not None:
            use_langgraph = bool(use_langgraph)

        run_all = bool(payload.get("run_all"))
        if run_all:
            use_langgraph = False

        graph_state = payload.get("state")
        if graph_state is not None and not isinstance(graph_state, dict):
            return {"ok": False, "error": "state must be a JSON object"}

        team_id = str(payload.get("team_id") or "").strip()
        if run_all and not team_id:
            from .swarm_graph import _load_swarm_state
            from .swarm_robots import _profile_team_map, _resolve_swarm_team_meta

            state_probe = (
                graph_state
                if isinstance(graph_state, dict)
                else _load_swarm_state(swarm_name)
            )
            if isinstance(state_probe, dict):
                team_id = str(state_probe.get("team_id") or "").strip()
            if not team_id:
                team_map = _profile_team_map(self, actor)
                meta = _resolve_swarm_team_meta(
                    state_probe if isinstance(state_probe, dict) else {"name": swarm_name},
                    team_map,
                )
                team_id = str(meta.get("team_id") or "").strip()

        agent_cards: dict[str, list[dict[str, Any]]] = {}
        team_ctx: dict[str, Any] = {}
        use_team_cards = payload.get("use_team_cards")
        if use_team_cards is None:
            use_team_cards = bool(run_all and team_id)
        else:
            use_team_cards = bool(use_team_cards)

        if run_all and team_id and use_team_cards:
            from .swarm_robots import prepare_swarm_test_team_context

            max_cards = payload.get("max_cards")
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
                agent_cards = dict(team_ctx.get("agent_cards") or {})
            elif not objective:
                return team_ctx

        if not objective:
            return {"ok": False, "error": "objective is required"}

        force = bool(payload.get("force") or payload.get("force_run"))
        stale_seconds = float(
            getattr(self.cfg.swarm_routing, "checkpoint_stale_seconds", 7200.0) or 7200.0
        )
        from .swarm_graph import guard_swarm_graph_run

        blocked = guard_swarm_graph_run(
            swarm_name, force=force, stale_seconds=stale_seconds
        )
        if blocked:
            return blocked

        exec_ctx = self._start_swarm_execution_record(
            swarm_name=swarm_name,
            objective=objective,
            payload=payload,
            team_ctx=team_ctx,
            user=user,
            team_id=team_id,
        )
        result_holder: list[dict[str, Any]] = [
            {"ok": False, "status": "error", "error": "interrupted"}
        ]
        from .whatsapp_send_policy import CONTEXT_SWARM, whatsapp_outbound_scope

        prev_swarm = os.environ.get("QCLAW_SWARM_SESSION")
        os.environ["QCLAW_SWARM_SESSION"] = "1"
        progress_job_id = str(payload.get("progress_job_id") or "").strip() or None
        self._announce_swarm_run_progress(
            progress_job_id,
            swarm_name=swarm_name,
            team_ctx=team_ctx if isinstance(team_ctx, dict) else {},
        )
        try:
            with whatsapp_outbound_scope(CONTEXT_SWARM):
                result_holder[0] = self._handle_swarm_graph_run_body(
                    payload=payload,
                    user=user,
                    actor=actor,
                    swarm_name=swarm_name,
                    objective=objective,
                    use_langgraph=use_langgraph,
                    run_all=run_all,
                    graph_state=graph_state,
                    team_id=team_id,
                    team_ctx=team_ctx,
                    agent_cards=agent_cards,
                )
                return result_holder[0]
        finally:
            if prev_swarm is None:
                os.environ.pop("QCLAW_SWARM_SESSION", None)
            else:
                os.environ["QCLAW_SWARM_SESSION"] = prev_swarm
            self._finish_swarm_execution_record(exec_ctx, result_holder[0])

    def _handle_swarm_graph_run_body(
        self,
        *,
        payload: dict,
        user: Optional[UserRecord],
        actor: Any,
        swarm_name: str,
        objective: str,
        use_langgraph: Optional[bool],
        run_all: bool,
        graph_state: Any,
        team_id: str,
        team_ctx: dict[str, Any],
        agent_cards: dict[str, list[dict[str, Any]]],
    ) -> dict:
        force = bool(payload.get("force") or payload.get("force_run"))
        stale_seconds = float(
            getattr(self.cfg.swarm_routing, "checkpoint_stale_seconds", 7200.0) or 7200.0
        )
        progress_job_id = str(payload.get("progress_job_id") or "").strip() or None
        on_run_progress = self._swarm_run_progress_callback(progress_job_id)
        engine_cfg = getattr(self.cfg, "swarm_ruflo_engine", None)
        from .ruflo_swarm_engine import build_ruflo_swarm_engine, use_ruflo_engine

        if use_ruflo_engine(
            engine_cfg, payload_engine=str(payload.get("engine") or "")
        ):
            engine = build_ruflo_swarm_engine(engine_cfg)
            if engine is None:
                return {"ok": False, "error": "ruflo engine not configured"}
            from .swarm_graph import _load_swarm_state, compile_swarm_graph

            state_probe = (
                graph_state
                if isinstance(graph_state, dict)
                else _load_swarm_state(swarm_name)
            )
            profiles: list[str] = []
            if isinstance(state_probe, dict):
                graph_probe = compile_swarm_graph(state_probe)
                profiles = list(graph_probe.order)
            from .ruflo_swarm_hooks import build_ruflo_swarm_hooks, swarm_run_light_mode
            from .swarm_memory import build_swarm_memory

            chat_cfg = self.cfg.openclaw_chat if self.cfg.openclaw_chat.enabled else None
            runner = self._build_swarm_llm_runner(
                swarm_name=swarm_name,
                team_ctx=team_ctx,
                payload=payload,
                model_override=self._swarm_cost_router(payload, chat_cfg),
                on_run_progress=on_run_progress,
            )
            memory = build_swarm_memory(self.cfg.swarm_memory)
            agent_cards = agent_cards if isinstance(agent_cards, dict) else None
            router, pause_before = self._swarm_routing_args(payload, chat_cfg, swarm_name)
            hitl_enabled = bool(
                self.cfg.swarm_routing.hitl_enabled
                or getattr(engine_cfg, "hitl_enabled", False)
            )

            ruflo_result = engine.run(
                swarm_name=swarm_name,
                objective=objective,
                profiles=profiles,
                hooks=build_ruflo_swarm_hooks(
                    self.cfg.swarm_ruflo_hooks,
                    light=swarm_run_light_mode(self.cfg, payload),
                ),
                node_runner=runner,
                memory=memory,
                memory_top_k=self.cfg.swarm_memory.retrieval_top_k,
                agent_cards=agent_cards,
                pause_before=pause_before,
                hitl_enabled=hitl_enabled,
                swarm_state=state_probe if isinstance(state_probe, dict) else None,
                a2a_cfg=getattr(self.cfg, "swarm_ruflo_a2a", None),
            )
            if ruflo_result.get("ok") or not getattr(
                engine_cfg, "fallback_to_hermes", True
            ):
                return self._maybe_evaluate_swarm_run(payload, ruflo_result)
            log.warning(
                "swarm graph: ruflo engine failed (%s); falling back to hermes",
                ruflo_result.get("error"),
            )

        from .swarm_graph import run_swarm_graph
        from .swarm_memory import build_swarm_memory
        from .ruflo_swarm_hooks import build_ruflo_swarm_hooks, wrap_router_with_ruflo

        chat_cfg = self.cfg.openclaw_chat if self.cfg.openclaw_chat.enabled else None
        runner = self._build_swarm_llm_runner(
            swarm_name=swarm_name,
            team_ctx=team_ctx,
            payload=payload,
            model_override=self._swarm_cost_router(payload, chat_cfg),
            on_run_progress=on_run_progress,
        )
        memory = build_swarm_memory(self.cfg.swarm_memory)
        ruflo_hooks = build_ruflo_swarm_hooks(
            self.cfg.swarm_ruflo_hooks,
            light=swarm_run_light_mode(self.cfg, payload),
        )
        router, pause_before = self._swarm_routing_args(payload, chat_cfg, swarm_name)
        if run_all:
            router = None
            pause_before = None
        elif ruflo_hooks is not None and router is not None:
            router = wrap_router_with_ruflo(router, ruflo_hooks)
        order_override = payload.get("order_override")
        if isinstance(order_override, list):
            order_override = [str(x).strip() for x in order_override if str(x).strip()]
        else:
            order_override = None
        on_node_progress = self._swarm_node_progress_callback(progress_job_id)
        result = run_swarm_graph(
            swarm_name,
            objective,
            node_runner=runner,
            state=graph_state if isinstance(graph_state, dict) else None,
            use_langgraph=use_langgraph,
            memory=memory,
            memory_top_k=self.cfg.swarm_memory.retrieval_top_k,
            router=router,
            pause_before=pause_before,
            max_steps=self.cfg.swarm_routing.max_steps,
            run_all=run_all,
            agent_cards=agent_cards or None,
            ruflo_hooks=ruflo_hooks,
            order_override=order_override,
            on_node_progress=on_node_progress,
            force=force,
            stale_seconds=stale_seconds,
        )

        if isinstance(result, dict) and result.get("ok"):
            self._invalidate_swarm_robots_cache()
            if team_ctx.get("ok"):
                result.setdefault("team_id", team_ctx.get("team_id"))
                result.setdefault("team_name", team_ctx.get("team_name"))
                result["cards_count"] = team_ctx.get("cards_count", 0)
                if team_ctx.get("selected_card_id"):
                    result["selected_card_id"] = team_ctx["selected_card_id"]
                if team_ctx.get("selected_card_ids"):
                    result["selected_card_ids"] = team_ctx["selected_card_ids"]
                if team_ctx.get("assignments_applied"):
                    result["assignments_applied"] = team_ctx["assignments_applied"]
                if run_all:
                    from .swarm_robots import finalize_swarm_kanban_cards

                    completed_ids = finalize_swarm_kanban_cards(
                        self,
                        team_ctx=team_ctx,
                        run_result=result,
                    )
                    if completed_ids:
                        result["kanban_completed"] = completed_ids

        result = self._maybe_evaluate_swarm_run(payload, result)
        return result

    def _swarm_routing_args(
        self, payload: dict, chat_cfg: Any, swarm_name: str
    ) -> tuple[Any, Optional[list[str]]]:
        """Resolve Phase 4 router + HITL pause list from config + payload."""
        routing = self.cfg.swarm_routing

        pause_before: list[str] = []
        if routing.hitl_enabled:
            pause_before.extend(routing.require_approval_for)
        extra = payload.get("pause_before")
        if isinstance(extra, list):
            pause_before.extend(str(x) for x in extra if str(x).strip())
        pause_before = list(dict.fromkeys(pause_before)) or None

        router = None
        want_router = payload.get("conditional_handoff")
        want_router = routing.conditional_handoff if want_router is None else bool(want_router)
        if want_router:
            from .swarm_graph import build_llm_router, first_candidate_router

            if routing.router_backend == "first":
                router = first_candidate_router
            else:
                router = build_llm_router(
                    chat_cfg=chat_cfg,
                    agent_cfg=self.cfg.agent,
                    swarm_name=swarm_name,
                )
        return router, pause_before

    def _swarm_cost_router(self, payload: dict, chat_cfg: Any) -> Any:
        """Phase 4 cost/quality routing — per-node model override (or None)."""
        routing = self.cfg.swarm_routing
        want = payload.get("cost_quality_routing")
        want = routing.cost_quality_routing if want is None else bool(want)
        if not want or chat_cfg is None:
            return None
        from .cost_router import build_swarm_cost_router

        return build_swarm_cost_router(chat_cfg=chat_cfg, cfg=routing, enabled=True)

    def _maybe_evaluate_swarm_run(self, payload: dict, result: dict) -> dict:
        """Phase 3 evaluation hook (skipped for paused/rejected runs).

        R7: evaluation always runs for completed runs (ok=True) to build
        eval history for trend analysis.  The config flag ``swarm_eval.enabled``
        still controls threshold enforcement, but the eval itself is mandatory.
        """
        if result.get("paused") or result.get("rejected"):
            return result

        eval_cfg = self.cfg.swarm_eval
        want_eval = payload.get("evaluate")
        # R7: always evaluate successful runs — config only gates enforcement
        want_eval = True if (want_eval is None and result.get("ok")) else (
            eval_cfg.enabled if want_eval is None else bool(want_eval)
        )
        if want_eval and result.get("ok"):
            try:
                from .swarm_eval import append_eval_history, evaluate_swarm_run

                report = evaluate_swarm_run(
                    result,
                    threshold=eval_cfg.threshold,
                    node_floor=eval_cfg.node_floor,
                    min_chars=eval_cfg.min_chars,
                    backend=eval_cfg.backend,
                )
                report = append_eval_history(
                    report,
                    store_dir=eval_cfg.store_dir,
                    regression_delta=eval_cfg.regression_delta,
                )
                result["evaluation"] = report
            except Exception as exc:
                log.warning("swarm graph: evaluation failed: %s", exc)
        return result

    def handle_swarm_graph_resume(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Resume a paused swarm run after human approval (Phase 4 HITL).

        Payload: ``{"swarm_name": "...", "approvals": {"<node>": true|false}}``.
        An approved node runs and the flow continues; a rejected node stops the
        run with status ``rejected``.
        """
        blocked = self._model_quota_guard()
        if blocked is not None:
            return blocked
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes or not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled in config"}

        swarm_name = str(payload.get("swarm_name") or payload.get("name") or "").strip()
        if not swarm_name:
            return {"ok": False, "error": "swarm_name is required"}

        approvals_raw = payload.get("approvals")
        approve_one = payload.get("approve")
        reject_one = payload.get("reject")
        approvals: dict[str, bool] = {}
        if isinstance(approvals_raw, dict):
            approvals = {str(k): bool(v) for k, v in approvals_raw.items()}
        if isinstance(approve_one, str) and approve_one.strip():
            approvals[approve_one.strip()] = True
        if isinstance(reject_one, str) and reject_one.strip():
            approvals[reject_one.strip()] = False
        if not approvals:
            return {"ok": False, "error": "approvals is required"}

        from .swarm_graph import load_checkpoint

        cp = load_checkpoint(swarm_name) or {}
        objective = str(cp.get("objective") or payload.get("objective") or "").strip()
        exec_ctx = self._start_swarm_execution_record(
            swarm_name=swarm_name,
            objective=objective,
            payload=payload,
            team_ctx={},
            user=user,
            resume=True,
        )
        result_holder: list[dict[str, Any]] = [
            {"ok": False, "status": "error", "error": "interrupted"}
        ]
        try:
            result_holder[0] = self._handle_swarm_graph_resume_body(
                payload=payload,
                user=user,
                swarm_name=swarm_name,
                approvals=approvals,
            )
            return result_holder[0]
        finally:
            self._finish_swarm_execution_record(exec_ctx, result_holder[0])

    def _handle_swarm_graph_resume_body(
        self,
        *,
        payload: dict,
        user: Optional[UserRecord],
        swarm_name: str,
        approvals: dict[str, bool],
    ) -> dict:
        from .swarm_graph import load_checkpoint

        cp = load_checkpoint(swarm_name)
        if cp is None:
            cp = {}
        progress_job_id = str(payload.get("progress_job_id") or "").strip() or None
        on_run_progress = self._swarm_run_progress_callback(progress_job_id)
        if isinstance(cp, dict) and str(cp.get("engine") or "") == "ruflo":
            from .ruflo_swarm_engine import build_ruflo_swarm_engine
            from .swarm_memory import build_swarm_memory
            from .ruflo_swarm_hooks import build_ruflo_swarm_hooks, swarm_run_light_mode

            engine = build_ruflo_swarm_engine(self.cfg.swarm_ruflo_engine)
            if engine is None:
                return {"ok": False, "error": "ruflo engine not configured"}
            chat_cfg = self.cfg.openclaw_chat if self.cfg.openclaw_chat.enabled else None
            runner = self._build_swarm_llm_runner(
                swarm_name=swarm_name,
                checkpoint=cp,
                payload=payload,
                model_override=self._swarm_cost_router(payload, chat_cfg),
                on_run_progress=on_run_progress,
            )
            result = engine.resume(
                swarm_name=swarm_name,
                approvals=approvals,
                node_runner=runner,
                memory=build_swarm_memory(self.cfg.swarm_memory),
                memory_top_k=self.cfg.swarm_memory.retrieval_top_k,
                hooks=build_ruflo_swarm_hooks(
                    self.cfg.swarm_ruflo_hooks,
                    light=swarm_run_light_mode(self.cfg, payload),
                ),
                a2a_cfg=getattr(self.cfg, "swarm_ruflo_a2a", None),
            )
            return self._maybe_evaluate_swarm_run(payload, result)

        from .swarm_graph import resume_swarm_graph
        from .swarm_memory import build_swarm_memory
        from .ruflo_swarm_hooks import build_ruflo_swarm_hooks, wrap_router_with_ruflo

        chat_cfg = self.cfg.openclaw_chat if self.cfg.openclaw_chat.enabled else None
        runner = self._build_swarm_llm_runner(
            swarm_name=swarm_name,
            checkpoint=cp,
            payload=payload,
            model_override=self._swarm_cost_router(payload, chat_cfg),
            on_run_progress=on_run_progress,
        )
        memory = build_swarm_memory(self.cfg.swarm_memory)
        ruflo_hooks = build_ruflo_swarm_hooks(
            self.cfg.swarm_ruflo_hooks,
            light=swarm_run_light_mode(self.cfg, payload),
        )
        router, pause_before = self._swarm_routing_args(payload, chat_cfg, swarm_name)
        if ruflo_hooks is not None and router is not None:
            router = wrap_router_with_ruflo(router, ruflo_hooks)
        result = resume_swarm_graph(
            swarm_name,
            node_runner=runner,
            approvals=approvals,
            memory=memory,
            memory_top_k=self.cfg.swarm_memory.retrieval_top_k,
            router=router,
            pause_before=pause_before,
            max_steps=self.cfg.swarm_routing.max_steps,
            ruflo_hooks=ruflo_hooks,
        )
        result = self._maybe_evaluate_swarm_run(payload, result)
        return result

    def handle_swarm_graph_overview(
        self, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        """Read-only overview of every swarm compiled as a LangGraph flow.

        Returns each swarm's nodes, ``handoff_to`` edges, entry point and the
        last-run status from ``.stack/swarms/<name>/graph_state.json`` so the UI
        can render the business flows.
        """
        from .swarm_graph import swarm_graphs_overview

        try:
            return swarm_graphs_overview()
        except Exception as e:  # pragma: no cover - defensive
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

