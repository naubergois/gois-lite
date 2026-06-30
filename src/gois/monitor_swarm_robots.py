"""Swarm robots snapshot, teams board, and card claim."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Optional

from .accounts import UserRecord

log = logging.getLogger(__name__)


def _swarm_robots_query_light(query: Optional[Mapping[str, Any]]) -> bool:
    if not query:
        return False
    return str(query.get("light") or "").strip().lower() in ("1", "true", "yes", "on")


class MonitorSwarmRobotsMixin:
    def _swarm_robots_cache_ttl(self) -> float:
        cfg = getattr(self.cfg, "swarm_robots", None)
        return max(0.0, float(getattr(cfg, "cache_seconds", 20.0) or 20.0))

    def _swarm_robots_stale_grace_seconds(self) -> float:
        """Serve expired cache while a background rebuild runs (stale-while-revalidate)."""
        return max(self._swarm_robots_cache_ttl() * 6.0, 60.0)

    def _swarm_robots_sse_chunk_size(self) -> int:
        cfg = getattr(self.cfg, "swarm_robots", None)
        return max(1, int(getattr(cfg, "sse_chunk_size", 8) or 8))

    def _invalidate_swarm_robots_cache(self) -> None:
        self._swarm_robots_cache = {}

    def _swarm_robots_disk_cache_path(self, key: str) -> Path:
        from .local_paths import project_stack_root

        safe = re.sub(r"[^\w.-]+", "_", key)
        cache_dir = project_stack_root() / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"swarm_robots_{safe}.json"

    def _read_swarm_robots_disk_cache(
        self, key: str
    ) -> Optional[tuple[float, dict[str, Any]]]:
        path = self._swarm_robots_disk_cache_path(key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        saved_at = float(raw.get("saved_at") or 0)
        snapshot = raw.get("snapshot")
        if saved_at <= 0 or not isinstance(snapshot, dict):
            return None
        max_age = self._swarm_robots_stale_grace_seconds() * 3.0
        if time.time() - saved_at > max_age:
            return None
        return saved_at, dict(snapshot)

    def _write_swarm_robots_disk_cache(
        self, key: str, snapshot: dict[str, Any]
    ) -> None:
        if not snapshot.get("ok", True) or snapshot.get("light"):
            return
        path = self._swarm_robots_disk_cache_path(key)
        payload = {"saved_at": time.time(), "snapshot": snapshot}
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(payload, default=str, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:
            log.debug("swarm robots disk cache write failed: %s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _store_swarm_robots_cache(
        self, key: str, snapshot: dict[str, Any]
    ) -> None:
        self._swarm_robots_cache[key] = (
            time.time() + self._swarm_robots_cache_ttl(),
            dict(snapshot),
        )
        self._write_swarm_robots_disk_cache(key, snapshot)

    def _schedule_swarm_robots_refresh(
        self, user: Optional[UserRecord], *, light: bool = False
    ) -> None:
        """Rebuild snapshot in the background without blocking GET /swarm/robots."""
        key = self._swarm_robots_cache_key(user, light=light)
        refreshing = getattr(self, "_swarm_robots_refresh_keys", None)
        if refreshing is None:
            refreshing = set()
            self._swarm_robots_refresh_keys = refreshing
        if key in refreshing:
            return
        refreshing.add(key)

        def _run() -> None:
            from .swarm_robots import build_swarm_robots_snapshot

            try:
                if not self._swarm_robots_cache_lock.acquire(blocking=False):
                    return
                try:
                    now = time.time()
                    entry = self._swarm_robots_cache.get(key)
                    if entry is not None and now < entry[0]:
                        return
                    snapshot = build_swarm_robots_snapshot(self, user, light=light)
                    if snapshot.get("ok", True):
                        self._store_swarm_robots_cache(key, snapshot)
                finally:
                    self._swarm_robots_cache_lock.release()
            except Exception as exc:
                log.warning("swarm robots background refresh failed: %s", exc)
            finally:
                refreshing.discard(key)

        threading.Thread(target=_run, daemon=True, name=f"swarm-robots-refresh:{key}").start()

    def _swarm_robots_cache_key(
        self, user: Optional[UserRecord], *, light: bool = False
    ) -> str:
        try:
            actor = self._accounts_actor(user)
        except Exception:
            actor = None
        base = str(getattr(actor, "id", None) or "anon")
        return f"{base}:light" if light else base

    def handle_swarm_robots_snapshot(
        self,
        user: Optional[UserRecord] = None,
        query: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Visual snapshot of swarm robots, profiles, and kanban cards.

        Cached briefly (TTL, keyed per user) so frequent polling and multiple
        open tabs reuse the same heavy computation instead of recomputing per
        request. The view depends on the user's profiles/teams/kanban, so the
        cache is partitioned per actor to avoid cross-user leakage.
        """
        from .swarm_robots import build_swarm_robots_snapshot

        light = _swarm_robots_query_light(query)
        key = self._swarm_robots_cache_key(user, light=light)
        now = time.time()
        entry = self._swarm_robots_cache.get(key)
        if entry is not None:
            expires_at, cached = entry
            if now < expires_at:
                return dict(cached)
            stale_until = expires_at + self._swarm_robots_stale_grace_seconds()
            if now < stale_until:
                stale = dict(cached)
                stale["stale"] = True
                self._schedule_swarm_robots_refresh(user, light=light)
                return stale

        if entry is None and not light:
            disk = self._read_swarm_robots_disk_cache(key)
            if disk is not None:
                _, cached = disk
                stale = dict(cached)
                stale["stale"] = True
                self._schedule_swarm_robots_refresh(user, light=light)
                return stale

        # Thundering-herd guard: if another thread is rebuilding, never block the UI.
        if not self._swarm_robots_cache_lock.acquire(blocking=False):
            if entry is not None:
                stale = dict(entry[1])
                stale["stale"] = True
                return stale
            return {
                "ok": True,
                "pending": True,
                "updated_at": now,
                "robots": [],
                "swarms": [],
                "summary": {
                    "robots": 0,
                    "swarms": 0,
                    "running": 0,
                    "working": 0,
                    "cards_assigned": 0,
                },
                "memory": {"enabled": False, "backend": "noop", "timelines": {}},
                "quality": {"enabled": False, "reports": {}},
            }
        try:
            now = time.time()
            entry = self._swarm_robots_cache.get(key)
            if entry is not None and now < entry[0]:
                return dict(entry[1])
            t0 = time.perf_counter()
            # Do not run _auto_start_stuck_doing_cards_tick here: it can invoke Hermes
            # cron runs synchronously and blocked /swarm/robots for 30s+. The kanban
            # cron sync loop already calls that tick on a background schedule.
            snapshot = build_swarm_robots_snapshot(self, user, light=light)
            elapsed = time.perf_counter() - t0
            metrics = getattr(self, "metrics", None)
            if metrics is not None:
                metrics.swarm_robots_build_duration_seconds.observe(elapsed)
            if snapshot.get("ok", True):
                self._store_swarm_robots_cache(key, snapshot)
            return snapshot
        except Exception as e:
            log.exception("swarm robots snapshot failed: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            self._swarm_robots_cache_lock.release()

    async def _startup_warm_swarm_robots_snapshot(self) -> None:
        """Seed cache from disk and refresh in background (non-blocking startup)."""
        if not self.cfg.hermes or not self.cfg.hermes_agent_create.enabled:
            return
        key = self._swarm_robots_cache_key(None)
        disk = self._read_swarm_robots_disk_cache(key)
        if disk is not None:
            _, snap = disk
            if snap.get("ok", True):
                self._swarm_robots_cache[key] = (
                    time.time() + self._swarm_robots_cache_ttl(),
                    dict(snap),
                )
        self._schedule_swarm_robots_refresh(None, light=False)

    def iter_swarm_robots_snapshot_sse(
        self, user: Optional[UserRecord] = None
    ) -> Iterator[dict[str, Any]]:
        """Yield SSE events: meta (fast) → robot chunks → complete snapshot."""
        from .swarm_robots import build_swarm_robots_meta, build_swarm_robots_snapshot

        key = self._swarm_robots_cache_key(user)
        now = time.time()
        entry = self._swarm_robots_cache.get(key)
        if entry is not None:
            expires_at, cached = entry
            if now < expires_at:
                out = dict(cached)
                out["phase"] = "complete"
                out["from_cache"] = True
                yield out
                return
            stale_until = expires_at + self._swarm_robots_stale_grace_seconds()
            if now < stale_until:
                out = dict(cached)
                out["phase"] = "complete"
                out["stale"] = True
                self._schedule_swarm_robots_refresh(user, light=False)
                yield out
                return

        if entry is None:
            disk = self._read_swarm_robots_disk_cache(key)
            if disk is not None:
                _, cached = disk
                out = dict(cached)
                out["phase"] = "complete"
                out["stale"] = True
                self._schedule_swarm_robots_refresh(user, light=False)
                yield out
                return

        inflight = getattr(self, "_swarm_robots_refresh_keys", None)
        if inflight is None:
            inflight = set()
            self._swarm_robots_refresh_keys = inflight
        if key in inflight:
            if entry is not None:
                stale = dict(entry[1])
                stale["phase"] = "complete"
                stale["stale"] = True
                yield stale
                return
            yield {
                "ok": True,
                "phase": "pending",
                "pending": True,
                "updated_at": now,
                "robots": [],
                "swarms": [],
                "summary": {"robots": 0, "swarms": 0, "running": 0, "working": 0},
            }
            return

        inflight.add(key)
        chunk_size = self._swarm_robots_sse_chunk_size()
        event_q: queue.Queue = queue.Queue()

        def _build() -> None:
            try:
                def _on_batch(
                    batch: list[dict[str, Any]], offset: int, total: int
                ) -> None:
                    event_q.put(
                        {
                            "ok": True,
                            "phase": "robots",
                            "robots": batch,
                            "offset": offset,
                            "total": total,
                        }
                    )

                t0 = time.perf_counter()
                snapshot = build_swarm_robots_snapshot(
                    self,
                    user,
                    light=False,
                    on_robots_batch=_on_batch,
                    robots_batch_size=chunk_size,
                )
                elapsed = time.perf_counter() - t0
                metrics = getattr(self, "metrics", None)
                if metrics is not None:
                    metrics.swarm_robots_build_duration_seconds.observe(elapsed)

                complete = dict(snapshot)
                complete["phase"] = "complete"
                if snapshot.get("ok", True):
                    if self._swarm_robots_cache_lock.acquire(timeout=30.0):
                        try:
                            self._store_swarm_robots_cache(key, snapshot)
                        finally:
                            self._swarm_robots_cache_lock.release()
                event_q.put(complete)
            except Exception as e:
                log.exception("swarm robots SSE snapshot failed: %s", e)
                event_q.put(
                    {
                        "ok": False,
                        "phase": "error",
                        "error": f"{type(e).__name__}: {e}",
                    }
                )
            finally:
                event_q.put(None)

        try:
            yield build_swarm_robots_meta(self, user)
            threading.Thread(
                target=_build, daemon=True, name=f"swarm-robots-sse:{key}"
            ).start()
            while True:
                event = event_q.get()
                if event is None:
                    break
                yield event
                if event.get("phase") in ("complete", "error"):
                    break
        finally:
            inflight.discard(key)

    @staticmethod
    def format_swarm_robots_sse_event(event: dict[str, Any]) -> bytes:
        payload = json.dumps(event, default=str, ensure_ascii=False)
        return f"data: {payload}\n\n".encode("utf-8")

    def handle_swarm_teams_board(
        self, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        """Teams + kanban cards + swarm agents for the Times e Swarm view."""
        from .swarm_robots import build_teams_swarm_board

        try:
            snapshot = self.handle_swarm_robots_snapshot(user)
            return build_teams_swarm_board(self, user, snapshot=snapshot)
        except Exception as e:
            log.exception("swarm teams board failed: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_swarm_teams_claim(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        """Assign a kanban card to a swarm agent, move to doing and schedule run."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes or not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled"}

        team_id = str(payload.get("team_id") or "").strip()
        task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
        assignee = str(payload.get("assignee") or payload.get("agent") or "").strip()
        if not team_id:
            return {"ok": False, "error": "team_id is required"}
        if not task_id:
            return {"ok": False, "error": "task_id is required"}
        if not assignee:
            return {"ok": False, "error": "assignee is required"}

        try:
            team = self.accounts.get_team(team_id, actor.id)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        workdir = str(payload.get("workdir") or "").strip()
        kanban_file = payload.get("kanban_file")
        board_data: Optional[dict[str, Any]] = None
        if not workdir:
            try:
                board_data = self.accounts.read_kanban(team.id, actor.id)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if isinstance(board_data, dict):
                workdir = str(board_data.get("workdir") or "").strip()
                if kanban_file is None and board_data.get("kanban_file"):
                    kanban_file = board_data.get("kanban_file")
        if not workdir:
            return {"ok": False, "error": "workdir is required"}

        task_row: Optional[dict[str, Any]] = None
        if isinstance(board_data, dict):
            task_row = next(
                (
                    row
                    for row in (board_data.get("tasks") or [])
                    if isinstance(row, dict)
                    and str(row.get("id") or "").strip() == task_id
                ),
                None,
            )
        if task_row is None:
            try:
                from .hermes_kanban import get_board

                board_data = get_board(
                    workdir,
                    self.cfg.hermes_agent_create,
                    kanban_file=str(kanban_file).strip() if kanban_file else None,
                )
                task_row = next(
                    (
                        row
                        for row in (board_data.get("tasks") or [])
                        if isinstance(row, dict)
                        and str(row.get("id") or "").strip() == task_id
                    ),
                    None,
                )
            except Exception:
                task_row = None

        delegated = False
        if isinstance(task_row, dict):
            from .swarm_robots import (
                resolve_delegate_assignee,
                team_leader_slug,
            )

            swarm_name = str(team.swarm_name or "").strip() or team.id
            swarm_state = self._ephemeral_team_swarm_state(team, swarm_name)
            agent_rows = [
                {"slug": str(s).strip(), "role": ""}
                for s in (team.profile_slugs or [])
                if str(s).strip()
            ]
            leader = team_leader_slug(
                list(team.profile_slugs or []),
                agent_rows,
                entry_agent=str(swarm_state.get("entry_agent") or ""),
            )
            load_counts: dict[str, int] = {}
            if isinstance(board_data, dict):
                from .hermes_kanban import normalize_assignees

                for row in board_data.get("tasks") or []:
                    if not isinstance(row, dict):
                        continue
                    for slug in normalize_assignees(
                        row.get("assignees") or row.get("assignee")
                    ):
                        load_counts[slug] = load_counts.get(slug, 0) + 1
            assignee, delegated = resolve_delegate_assignee(
                task_row,
                assignee,
                agent_rows,
                leader_slug=leader,
                load_counts=load_counts,
            )

        base = {
            "workdir": workdir,
            "team_id": team.id,
        }
        if kanban_file is not None:
            base["kanban_file"] = kanban_file

        assign_payload = {
            **base,
            "action": "assign_task",
            "task_id": task_id,
            "assignees": [assignee],
        }
        assign_result = self.handle_hermes_kanban_action(assign_payload, user)
        if not assign_result.get("ok"):
            return assign_result

        if delegated:
            note = (
                f"Delegado pelo líder do time para `{assignee}` "
                "(agente mais competente para esta tarefa)."
            )
            self.handle_hermes_kanban_action(
                {
                    **base,
                    "action": "update_task",
                    "task_id": task_id,
                    "task": {
                        "notes": note,
                    },
                },
                user,
            )

        move_to_doing = payload.get("move_to_doing", True)
        if isinstance(move_to_doing, str):
            move_to_doing = move_to_doing.strip().lower() not in (
                "0",
                "false",
                "no",
                "off",
            )
        if move_to_doing:
            move_payload = {
                **base,
                "action": "move_task",
                "task_id": task_id,
                "column": "doing",
            }
            move_result = self.handle_hermes_kanban_action(move_payload, user)
            if not move_result.get("ok"):
                return move_result

        run_now = payload.get("run_now", True)
        if isinstance(run_now, str):
            run_now = run_now.strip().lower() not in ("0", "false", "no", "off")

        schedule_result: dict[str, Any] = {"ok": True, "skipped": True}
        if run_now:
            schedule_result = self.handle_hermes_kanban_schedule(
                {
                    **base,
                    "task_id": task_id,
                    "assignee": assignee,
                    "once": True,
                    "async": True,
                    "schedule": str(payload.get("schedule") or "1m").strip() or "1m",
                },
                user,
            )
            if not schedule_result.get("ok"):
                return schedule_result

        self._invalidate_swarm_robots_cache()
        return {
            "ok": True,
            "team_id": team.id,
            "team_name": team.name,
            "task_id": task_id,
            "assignee": assignee,
            "delegated": delegated,
            "moved_to_doing": bool(move_to_doing),
            "schedule": schedule_result,
        }

