"""Structured Hermes cron diagnostics (no auto-fix)."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from .hermes_cron import (
    cron_output_dir_for_jobs_path,
    detect_running_cron_jobs,
    get_cron_job_result,
    hermes_cron_snapshot,
    hermes_home_from_jobs_path,
    is_transient_cron_scheduler_error,
    occupied_cron_minutes,
    probe_hermes_cron_scheduler,
    read_jobs_file,
    resolve_agent_log_path,
)
from .local_paths import hermes_home
from .recovery import parse_hermes_cron_job_name

_CRON_FAIL_LINE = re.compile(
    r"cron\.scheduler:\s*Job\s+'([^']+)'\s+failed",
    re.IGNORECASE,
)


def _tail_cron_failures(log_path: Path, *, max_lines: int = 400) -> list[dict[str, str]]:
    if not log_path.is_file():
        return []
    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    out: list[dict[str, str]] = []
    for line in reversed(lines):
        m = _CRON_FAIL_LINE.search(line)
        if not m:
            continue
        name = m.group(1).strip()
        out.append({"job_name": name, "line": line.strip()[:500]})
        if len(out) >= 15:
            break
    out.reverse()
    return out


def build_hermes_cron_diagnostic_report(
    *,
    jobs_path: Path,
    hermes_log_paths: list[str],
    job_id: Optional[str] = None,
    gateway_up: Optional[bool] = None,
) -> dict[str, Any]:
    """Build a read-only cron health report for dashboards and the diagnostic agent."""
    jobs_path = jobs_path.expanduser().resolve()
    jobs_home = hermes_home_from_jobs_path(jobs_path)
    env_home = Path(os.environ.get("HERMES_HOME") or hermes_home()).expanduser().resolve()

    issues: list[dict[str, Any]] = []
    recommendations: list[str] = []

    if not jobs_path.is_file():
        issues.append({
            "severity": "error",
            "code": "JOBS_FILE_MISSING",
            "message": f"Ficheiro de jobs não encontrado: {jobs_path}",
        })
        recommendations.append(
            "Confirme HERMES_HOME no .env do launchd e jobs_path no config.yaml."
        )

    if env_home != jobs_home:
        issues.append({
            "severity": "warning",
            "code": "HERMES_HOME_MISMATCH",
            "message": (
                f"HERMES_HOME do ambiente ({env_home}) difere do home dos jobs "
                f"({jobs_home}). Crons CLI podem apontar para outro conjunto de jobs."
            ),
            "env_hermes_home": str(env_home),
            "jobs_hermes_home": str(jobs_home),
        })
        recommendations.append(
            f"Alinhe HERMES_HOME={jobs_home} no .env ou ajuste hermes_cron_recovery.jobs_path."
        )

    probe = probe_hermes_cron_scheduler(jobs_path)
    if not probe.get("ok"):
        code = "SCHEDULER_BLOCKED" if probe.get("blocked") else "SCHEDULER_DOWN"
        issues.append({
            "severity": "error",
            "code": code,
            "message": probe.get("summary") or probe.get("reason") or "Scheduler inativo",
            "hermes_home": probe.get("hermes_home"),
        })
        recommendations.append(
            "Subir gateway para o HERMES_HOME dos jobs: "
            "`hermes --profile default gateway start` ou botão «garantir gateway» no dashboard."
        )

    if gateway_up is False:
        issues.append({
            "severity": "warning",
            "code": "GATEWAY_PROCESS_DOWN",
            "message": "Processo `hermes gateway run` não detetado pelo monitor (pgrep).",
        })

    snap: dict[str, Any] = {"ok": False}
    if jobs_path.is_file():
        agent_log = resolve_agent_log_path(hermes_log_paths)
        output_root = cron_output_dir_for_jobs_path(jobs_path)
        snap = hermes_cron_snapshot(
            jobs_path,
            agent_log_path=agent_log,
            output_root=output_root,
        )
        if int(snap.get("error_count") or 0) > 0:
            err_jobs = [
                j for j in (snap.get("jobs") or [])
                if isinstance(j, dict)
                and j.get("active")
                and j.get("last_status") == "error"
            ][:20]
            issues.append({
                "severity": "error",
                "code": "JOBS_LAST_RUN_ERROR",
                "message": f"{snap.get('error_count')} job(s) com última execução em erro.",
                "jobs": [
                    {
                        "id": j.get("id"),
                        "name": j.get("name"),
                        "last_run_at": j.get("last_run_at"),
                        "preview": (j.get("last_result_preview") or "")[:200],
                    }
                    for j in err_jobs
                ],
            })
            recommendations.append(
                "Abra o resultado do job no dashboard ou use «diagnosticar» com job_id; "
                "depois `hermes --profile default cron run <id>` se a falha for transitória."
            )

        stale_running = [
            r for r in (snap.get("running") or [])
            if isinstance(r, dict)
            and int(r.get("seconds_since_activity") or 0) > 600
        ]
        if stale_running:
            issues.append({
                "severity": "warning",
                "code": "STALE_RUNNING_CRONS",
                "message": f"{len(stale_running)} job(s) marcados como em execução há muito tempo.",
                "jobs": [
                    {"id": r.get("job_id"), "name": r.get("name")}
                    for r in stale_running[:10]
                ],
            })

        try:
            jobs_raw, _ = read_jobs_file(jobs_path)
            occupied = occupied_cron_minutes(jobs_raw)
            if len(occupied) >= 55:
                issues.append({
                    "severity": "info",
                    "code": "CRON_MINUTE_CONGESTION",
                    "message": (
                        f"{len(occupied)} minutos da hora com jobs sobrepostos "
                        "(pode atrasar ticks)."
                    ),
                })
        except Exception:
            pass

    log_failures: list[dict[str, str]] = []
    for raw in hermes_log_paths:
        p = Path(raw).expanduser()
        if p.name == "errors.log" or "errors" in p.name:
            log_failures.extend(_tail_cron_failures(p))
    if log_failures:
        issues.append({
            "severity": "error",
            "code": "RECENT_LOG_FAILURES",
            "message": f"{len(log_failures)} falha(s) recente(s) em errors.log.",
            "entries": log_failures,
        })
    shutdown_failures = [
        entry
        for entry in log_failures
        if is_transient_cron_scheduler_error(entry.get("line") or "")
    ]
    if shutdown_failures:
        issues.append({
            "severity": "warning",
            "code": "GATEWAY_SHUTDOWN_DURING_CRON",
            "message": (
                f"{len(shutdown_failures)} falha(s) por restart/shutdown do gateway "
                "durante execução de cron (erro transitório)."
            ),
            "entries": shutdown_failures,
        })
        recommendations.append(
            "Evite reiniciar o gateway ou o monitor enquanto crons correm; "
            "reexecute com `hermes --profile default cron run <id>` quando o gateway estiver estável."
        )

    job_detail: Optional[dict[str, Any]] = None
    jid = (job_id or "").strip()
    if jid and jobs_path.is_file():
        output_root = cron_output_dir_for_jobs_path(jobs_path)
        job_detail = get_cron_job_result(jid, output_root)
        if not job_detail.get("ok"):
            issues.append({
                "severity": "warning",
                "code": "JOB_OUTPUT_MISSING",
                "message": job_detail.get("error") or "Sem output guardado para o job.",
                "job_id": jid,
            })

    healthy = not any(i.get("severity") == "error" for i in issues) and probe.get("ok")
    if healthy and not issues:
        recommendations.append("Scheduler e jobs OK — nenhuma ação necessária.")

    return {
        "ok": True,
        "healthy": healthy,
        "generated_at": time.time(),
        "jobs_path": str(jobs_path),
        "hermes_home": str(jobs_home),
        "env_hermes_home": str(env_home),
        "scheduler": probe,
        "cron_snapshot": {
            "total": snap.get("total"),
            "active_count": snap.get("active_count"),
            "paused_count": snap.get("paused_count"),
            "error_count": snap.get("error_count"),
            "running_count": snap.get("running_count"),
        },
        "issues": issues,
        "recommendations": recommendations,
        "job_id": jid or None,
        "job_detail": job_detail,
    }


def hermes_cron_diagnostic_headline(report: Optional[dict[str, Any]]) -> str:
    """One-line summary for dashboards."""
    if not report:
        return "Diagnóstico cron indisponível"
    if report.get("healthy"):
        return "Crons Hermes OK — scheduler ativo"
    for severity in ("error", "warning", "info"):
        for issue in report.get("issues") or []:
            if issue.get("severity") == severity:
                msg = str(issue.get("message") or issue.get("code") or "").strip()
                if msg:
                    return msg
    return "Crons Hermes com problemas — ver detalhes abaixo"
