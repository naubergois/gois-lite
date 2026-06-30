"""Resolve background/system actor for kanban and priority queue."""

from __future__ import annotations

import logging

from pathlib import Path
from typing import Any, Optional

from .accounts import UserRecord

log = logging.getLogger(__name__)


class MonitorSystemActorMixin:

    def _system_actor(
        self, payload: Optional[dict[str, Any]] = None
    ) -> Optional[UserRecord]:
        """Resolve account for background kanban jobs (priority queue, auto-start)."""
        if not self.cfg.auth.enabled:
            return self.accounts.ensure_local_user()

        team_id = str((payload or {}).get("team_id") or "").strip()
        if team_id:
            for team in self.accounts.list_all_teams():
                if team.id == team_id:
                    owner = self.accounts.get_user_by_id(team.owner_id)
                    if owner is not None:
                        return owner

        workdir = str((payload or {}).get("workdir") or "").strip()
        if workdir:
            try:
                resolved = Path(workdir).expanduser().resolve()
            except OSError:
                resolved = None
            if resolved is not None:
                for team in self.accounts.list_all_teams():
                    for candidate in (
                        self.accounts.team_workdir(team),
                        self.accounts.team_dir(team.id),
                    ):
                        try:
                            base = candidate.resolve()
                        except OSError:
                            continue
                        if resolved == base or self._path_under_or_equal(resolved, base):
                            owner = self.accounts.get_user_by_id(team.owner_id)
                            if owner is not None:
                                return owner

        for team in self.accounts.list_all_teams():
            owner = self.accounts.get_user_by_id(team.owner_id)
            if owner is not None:
                return owner

