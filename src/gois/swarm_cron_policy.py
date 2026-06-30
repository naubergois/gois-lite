"""Enforce Hermes cron: only jobs linked to swarms may stay active / run."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .hermes_cron import (
    cron_output_dir_for_jobs_path,
    find_job_by_id,
    hermes_cron_argv,
    read_jobs_file,
    run_hermes_cron_command,
    summarize_cron_job,
)
from .openai_swarm import load_swarms_full
from .swarm_robots import _swarm_links_for_profile_slug
from .swarm_robots_slugs import _norm_slug

log = logging.getLogger(__name__)


def cron_job_belongs_to_swarm(job: dict[str, Any]) -> bool:
    """True when the cron job's Hermes profile is linked to a swarm definition."""
    if not isinstance(job, dict):
        return False
    profile = _norm_slug(str(job.get("profile") or ""))
    if not profile:
        return False
    swarm_names, _ = _swarm_links_for_profile_slug(profile)
    return bool(swarm_names)


def swarm_only_cron_block_reason(job: dict[str, Any]) -> str:
    profile = str(job.get("profile") or "").strip() or "?"
    name = str(job.get("name") or job.get("id") or "?")
    return (
        f"cron '{name}' (perfil {profile}) não pertence a nenhum swarm — "
        "execução bloqueada pela política swarm-only"
    )


def enforce_swarm_only_cron_jobs(
    jobs_path: Path,
    *,
    accept_hooks: bool = False,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Pause every active cron job whose profile is not linked to a swarm."""
    jobs, _ = read_jobs_file(jobs_path)
    output_root = cron_output_dir_for_jobs_path(jobs_path)
    swarm_count = len(load_swarms_full())
    to_pause: list[tuple[str, str, str]] = []

    for job in jobs:
        if not isinstance(job, dict):
            continue
        summary = summarize_cron_job(job, output_root=output_root)
        if not summary.get("active"):
            continue
        if cron_job_belongs_to_swarm(job):
            continue
        jid = str(summary.get("id") or "").strip()
        if not jid:
            continue
        to_pause.append(
            (
                jid,
                str(summary.get("name") or jid),
                str(job.get("profile") or ""),
            )
        )

    if not to_pause:
        return {
            "ok": True,
            "paused_count": 0,
            "paused": [],
            "swarm_count": swarm_count,
            "jobs_path": str(jobs_path),
        }

    failures: list[dict[str, Any]] = []
    paused: list[dict[str, str]] = []
    pause_timeout = min(timeout_seconds, 60.0)
    for jid, job_name, profile in to_pause:
        cmd = hermes_cron_argv("pause", jid, accept_hooks=accept_hooks)
        result = run_hermes_cron_command(
            cmd,
            timeout_seconds=pause_timeout,
            job_id=jid,
            job_name=job_name,
            jobs_path=jobs_path,
        )
        row = {"job_id": jid, "job_name": job_name, "profile": profile}
        if result.get("ok"):
            paused.append(row)
        else:
            failures.append({**row, **result})

    if paused:
        log.info(
            "swarm-only cron: paused %d non-swarm job(s) (swarms=%d)",
            len(paused),
            swarm_count,
        )

    return {
        "ok": not failures,
        "paused_count": len(paused),
        "paused": paused,
        "failures": failures,
        "swarm_count": swarm_count,
        "jobs_path": str(jobs_path),
    }


def swarm_only_cron_guard(
    job: Optional[dict[str, Any]],
    *,
    jobs_path: Optional[Path] = None,
    job_id: str = "",
) -> Optional[dict[str, Any]]:
    """Return an error payload when *job* must not run/resume under swarm-only policy."""
    resolved = job
    if resolved is None and jobs_path is not None and job_id:
        resolved = find_job_by_id(job_id, jobs_path)
    if not isinstance(resolved, dict):
        return None
    if cron_job_belongs_to_swarm(resolved):
        return None
    return {
        "ok": False,
        "error": swarm_only_cron_block_reason(resolved),
        "swarm_only": True,
        "job_id": str(resolved.get("id") or job_id or ""),
        "profile": str(resolved.get("profile") or ""),
    }
