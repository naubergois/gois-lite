"""Student attendance (chamada de presença) request handlers.

Bridges the ``/attendance`` HTTP endpoints to :mod:`gois.attendance_store`.
"""

from __future__ import annotations

from typing import Optional

from .accounts import UserRecord


class MonitorAttendanceMixin:
    def _attendance_auth(self, user: Optional[UserRecord]) -> Optional[dict]:
        if self.cfg.auth.enabled and user is None:
            return {"ok": False, "error": "not authenticated"}
        return None

    def handle_attendance_save(
        self, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._attendance_auth(user)
        if denied:
            return denied
        from .attendance_store import save_attendance

        return save_attendance(
            turma=str(payload.get("turma") or ""),
            data=str(payload.get("data") or ""),
            registros=payload.get("registros"),
            disciplina=str(payload.get("disciplina") or ""),
            professor=str(payload.get("professor") or ""),
        )

    def handle_attendance_get(
        self, query: dict, user: Optional[UserRecord]
    ) -> dict:
        denied = self._attendance_auth(user)
        if denied:
            return denied
        from .attendance_store import get_attendance, list_attendance

        turma = str(query.get("turma") or "").strip()
        data = str(query.get("data") or "").strip()
        disciplina = str(query.get("disciplina") or "").strip()
        # A specific session needs both turma and data; otherwise list.
        if turma and data:
            return get_attendance(turma, data, disciplina)
        return list_attendance(turma=turma or None, data=data or None)
