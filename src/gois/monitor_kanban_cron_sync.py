"""Kanban ↔ Hermes cron sync loop and tick."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class MonitorKanbanCronSyncMixin:

    async def _kanban_cron_sync_loop(self) -> None:
        """Periodically sync hermes cron job results back to kanban cards.

        For every cron job that has a ``cron_job_id`` stored on a kanban card,
        detect state transitions and update the card:
        - running  → move card to ``doing`` (if not already there)
        - ok       → move card to ``done`` + add completion note
        - error    → add error note to card (stays in ``doing`` so the team can act)
        """
        # Stagger startup so we don't collide with heavy boot-up tasks.
        await asyncio.sleep(60.0)
        while True:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, self._kanban_cron_sync_tick
                )
            except Exception as exc:
                log.warning("kanban cron sync tick failed: %s", exc)
            await asyncio.sleep(60.0)

    def _kanban_cron_sync_tick(self) -> None:
        """One synchronous pass of the kanban ↔ cron sync."""
        if not self.cfg.hermes or not self.cfg.hermes_agent_create.enabled:
            return

        # --- 1. Collect all kanban boards with cron-linked cards ---
        boards = self._collect_kanban_boards_with_cron_cards()

        # Build a lookup: cron_job_id → list of (board_info, task)
        cron_to_cards: dict[str, list[tuple[dict, dict]]] = {}
        for board_info in boards:
            for task in board_info.get("tasks") or []:
                cjid = str(task.get("cron_job_id") or "").strip()
                if cjid:
                    cron_to_cards.setdefault(cjid, []).append((board_info, task))

        # --- 2. Get current cron snapshot (uses cache, cheap) ---
        snap = self._cached_hermes_cron_snapshot()
        all_jobs: list[dict] = list(snap.get("jobs") or [])
        running_job_ids = {
            str(row.get("job_id") or "").strip()
            for row in (snap.get("running") or [])
            if isinstance(row, dict) and str(row.get("job_id") or "").strip()
        }

        # --- 2.1 Rule: keep unallocated cron jobs in a dedicated scheduling team ---
        # Returns the scheduling board info (if any cards were created/exist) so we
        # can merge it into cron_to_cards and track status transitions for those cards
        # in the same tick — otherwise newly-created scheduling cards would never
        # transition to running/done/error until the *next* tick.
        scheduling_board_info = self._ensure_unassigned_cron_jobs_scheduling_cards(
            all_jobs, cron_to_cards
        )
        if scheduling_board_info:
            for task in scheduling_board_info.get("tasks") or []:
                cjid = str(task.get("cron_job_id") or "").strip()
                if cjid and cjid not in cron_to_cards:
                    cron_to_cards.setdefault(cjid, []).append(
                        (scheduling_board_info, task)
                    )

        if cron_to_cards and all_jobs:
            # --- 3. Detect status changes and apply kanban updates ---
            prev_states = self._kanban_cron_job_last_status
            new_states: dict[str, str] = {}

            for job in all_jobs:
                job_id = str(job.get("id") or "").strip()
                if not job_id or job_id not in cron_to_cards:
                    continue

                if job_id in running_job_ids or bool(job.get("running")):
                    current_status = "running"
                else:
                    current_status = str(job.get("last_status") or "").strip()

                new_states[job_id] = current_status
                prev_status = prev_states.get(job_id, "")

                if current_status == prev_status:
                    continue  # no change

                cards = cron_to_cards[job_id]
                for board_info, task in cards:
                    self._apply_cron_status_to_card(
                        board_info=board_info,
                        task=task,
                        job=job,
                        current_status=current_status,
                        prev_status=prev_status,
                    )

            # Persist new states (only tracked jobs)
            self._kanban_cron_job_last_status = {**prev_states, **new_states}

        self._auto_start_stuck_doing_cards_tick()
        self._delegate_unassigned_team_cards_tick()
        self._repair_doing_cards_with_cron_ok(
            self._collect_kanban_board_infos(require_cron_link=False),
            all_jobs,
            running_job_ids,
        )

