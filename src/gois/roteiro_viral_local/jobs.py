"""In-memory job store for local RV thumbnail/image jobs."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Optional


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(
        self,
        *,
        job_type: str,
        topic: str,
        request_payload: dict[str, Any],
        parent_job_id: str = "",
        api_key: str = "",
    ) -> str:
        job_id = f"thumbnail_{uuid.uuid4()}"
        now = time.time()
        job = {
            "job_id": job_id,
            "parent_job_id": parent_job_id or None,
            "topic": topic[:80],
            "type": job_type,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "api_key": api_key,
            "logs": ["🚀 Job local enfileirado..."],
            "request_payload": dict(request_payload),
            "final_state": {},
        }
        with self._lock:
            self._jobs[job_id] = job
        return job_id

    def get(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def log(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            logs = job.setdefault("logs", [])
            logs.append(message)
            job["updated_at"] = time.time()

    def update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.update(fields)
            job["updated_at"] = time.time()

    def set_status(self, job_id: str, status: str, *, error: str = "") -> None:
        patch: dict[str, Any] = {"status": status}
        if error:
            patch["error"] = error
        self.update(job_id, **patch)

    def run_async(self, job_id: str, worker: Callable[[str, dict[str, Any]], None]) -> None:
        job = self.get(job_id)
        if not job:
            return

        def _run() -> None:
            try:
                self.set_status(job_id, "running")
                worker(job_id, job)
            except Exception as exc:
                self.log(job_id, f"❌ Erro: {exc}")
                self.set_status(job_id, "failed", error=str(exc))

        threading.Thread(target=_run, daemon=True).start()


STORE = JobStore()
