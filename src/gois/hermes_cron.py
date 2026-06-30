"""Read Hermes cron jobs from ~/.hermes/cron/jobs.json for status and dashboard."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    from croniter import croniter
except ImportError:
    croniter = None

from .local_paths import hermes_home, hermes_source
from .whatsapp_allowlist import cron_job_whatsapp_delivery

# Job ids are UUID fragments with hyphens (e.g. 0e2c71f7-8f6, f062f5d6-888).
_CRON_SESSION_RE = re.compile(r"\[(cron_[a-f0-9\-]+_\d{8}_\d{6})\]")
_JOB_ID_FROM_SESSION_RE = re.compile(r"^cron_([a-f0-9\-]+)_\d{8}_\d{6}$")
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_SESSION_STARTED_AT_RE = re.compile(r"_(\d{8})_(\d{6})$")

_HERMES_BIN: Optional[str] = None
MANUAL_CRON_RUN_DELAY_SECONDS = 60


def _augmented_path() -> str:
    """PATH with common Hermes install locations (launchd often omits ~/.local/bin)."""
    home = Path.home()
    extra = [
        str(home / ".local/bin"),
        # Self-contained install: vendored Hermes source with its own venv.
        str(hermes_source() / "venv/bin"),
        # Legacy: Hermes venv co-located with the state directory.
        str(hermes_home() / "hermes-agent/venv/bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    current = os.environ.get("PATH", "")
    parts = extra + ([current] if current else [])
    return os.pathsep.join(dict.fromkeys(parts))


def resolve_hermes_python() -> str:
    """Python interpreter bundled with the resolved Hermes CLI (venv)."""
    bin_path = Path(resolve_hermes_bin()).expanduser().resolve()
    for candidate in (
        bin_path.parent / "python3",
        bin_path.parent / "python",
        Path.home() / ".hermes/hermes-agent/venv/bin/python3",
        Path.home() / ".hermes/hermes-agent/venv/bin/python",
        hermes_home() / "hermes-agent/venv/bin/python3",
        hermes_home() / "hermes-agent/venv/bin/python",
        hermes_source() / "venv/bin/python3",
        hermes_source() / "venv/bin/python",
    ):
        if candidate.is_file():
            return str(candidate)
    import sys

    return sys.executable


def resolve_hermes_bin() -> str:
    """Return the Hermes CLI path, searching beyond launchd's minimal PATH."""
    global _HERMES_BIN
    if _HERMES_BIN:
        return _HERMES_BIN

    explicit = os.environ.get("HERMES_BIN")
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            _HERMES_BIN = str(p)
            return _HERMES_BIN

    found = shutil.which("hermes", path=_augmented_path())
    if found:
        _HERMES_BIN = found
        return _HERMES_BIN

    for candidate in (
        Path.home() / ".local/bin/hermes",
        hermes_source() / "venv/bin/hermes",
        hermes_home() / "hermes-agent/venv/bin/hermes",
        Path("/opt/homebrew/bin/hermes"),
        Path("/usr/local/bin/hermes"),
    ):
        if candidate.is_file():
            _HERMES_BIN = str(candidate)
            return _HERMES_BIN

    _HERMES_BIN = "hermes"
    return _HERMES_BIN


def hermes_cmd(*parts: str) -> list[str]:
    """Build argv for a Hermes CLI invocation."""
    return [resolve_hermes_bin(), *parts]


# Hermes CLI reads active_profile and rewrites HERMES_HOME to that profile's
# directory, which usually has no cron/jobs.json. Force the default (root)
# profile so cron subcommands use the same jobs file as gois.
_HERMES_CRON_CLI_PROFILE = ("--profile", "default")

_CLI_FAILURE_PREFIX = "Failed to "
_ISO_TIMESTAMP_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
)


def hermes_home_from_jobs_path(jobs_path: Path) -> Path:
    """Return the Hermes home directory that owns ``cron/jobs.json``."""
    return jobs_path.expanduser().resolve().parent.parent


def hermes_cron_argv(
    action: str,
    *args: str,
    accept_hooks: bool = False,
    model: Optional[str] = None,
) -> list[str]:
    """Build `hermes --profile default [--model X] cron [--accept-hooks] <action> …`."""
    top_flags = list(_HERMES_CRON_CLI_PROFILE)
    if model:
        top_flags.extend(["--model", model])
    cmd = hermes_cmd(*top_flags, "cron")
    if accept_hooks:
        cmd.append("--accept-hooks")
    cmd.append(action)
    cmd.extend(args)
    return cmd


def hermes_gateway_argv(*args: str) -> list[str]:
    """Build `hermes --profile default gateway …` for the stack cron home."""
    return hermes_cmd(*_HERMES_CRON_CLI_PROFILE, "gateway", *args)


def hermes_dashboard_argv(*extra: str) -> list[str]:
    """Build `hermes --profile default dashboard …` (same home as cron jobs)."""
    return hermes_cmd(*_HERMES_CRON_CLI_PROFILE, "dashboard", *extra)


def resolve_hermes_cron_jobs_path(configured: Optional[str] = None) -> Path:
    """Prefer ``HERMES_HOME/cron/jobs.json`` over a stale ``.stack`` path in config."""
    home_jobs = hermes_home() / "cron" / "jobs.json"
    if home_jobs.is_file():
        return home_jobs
    if configured:
        cfg_jobs = Path(configured).expanduser()
        if cfg_jobs.is_file():
            return cfg_jobs
    return home_jobs


def _hermes_cli_output_indicates_failure(stdout: str, stderr: str) -> bool:
    for text in (stdout, stderr):
        for line in (text or "").splitlines():
            if line.strip().startswith(_CLI_FAILURE_PREFIX):
                return True
    return False


def _summarize_hermes_cron_output(stdout: str, stderr: str, rc: int) -> str:
    """Pick a human-readable line; Hermes often prints only a timestamp on stderr."""
    for text in (stderr, stdout):
        for line in reversed((text or "").strip().splitlines()):
            line = line.strip()
            if not line:
                continue
            if line.startswith(_CLI_FAILURE_PREFIX):
                return line[:300]
            if _ISO_TIMESTAMP_LINE.match(line):
                continue
            if any(
                line.startswith(prefix)
                for prefix in (
                    "Triggered job:",
                    "Created job:",
                    "Updated job:",
                    "Paused job:",
                    "Resumed job:",
                    "Removed job:",
                )
            ):
                return line[:300]
            return line[:300]
    return f"rc={rc}"


def _cron_jobs_store_key(jobs_path: Path) -> str:
    home_name = hermes_home_from_jobs_path(jobs_path).name or "default"
    return f"hermes:cron:{home_name}:jobs"


def _parse_jobs_payload(data: Any) -> tuple[list[dict[str, Any]], Optional[str]]:
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict):
        jobs = data.get("jobs")
        if isinstance(jobs, list):
            updated = data.get("updated_at")
            return jobs, str(updated) if updated is not None else None
    return [], None


def _read_jobs_payload_from_disk(jobs_path: Path) -> Optional[Any]:
    if not jobs_path.is_file():
        return None
    try:
        return json.loads(jobs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _sync_cron_jobs_blob_from_disk(jobs_path: Path) -> None:
    """Refresh Mongo when Hermes CLI updated jobs.json directly on disk."""
    jobs_path = jobs_path.expanduser()
    raw = _read_jobs_payload_from_disk(jobs_path)
    if raw is None:
        return
    try:
        from .runtime_blobs_mongo import blob_get_updated_at, blob_set, runtime_mongo_enabled

        if not runtime_mongo_enabled():
            return
        mtime = jobs_path.stat().st_mtime
        key = _cron_jobs_store_key(jobs_path)
        if blob_get_updated_at(key) >= mtime:
            return
        blob_set(key, raw)
    except OSError:
        return


def read_jobs_file(jobs_path: Path) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Return (jobs list, updated_at). Mongo authoritative; disk legacy for Hermes CLI."""
    jobs_path = jobs_path.expanduser()
    _sync_cron_jobs_blob_from_disk(jobs_path)
    from .runtime_state import load_json

    raw = load_json(_cron_jobs_store_key(jobs_path), jobs_path)
    if raw is not None:
        return _parse_jobs_payload(raw)
    return [], None


def _build_jobs_payload(jobs_path: Path, jobs: list[dict[str, Any]]) -> Any:
    updated_at = datetime.now().astimezone().isoformat()
    raw = _read_jobs_payload_from_disk(jobs_path)
    if isinstance(raw, dict):
        payload = dict(raw)
        payload["jobs"] = jobs
        payload["updated_at"] = updated_at
        return payload
    if isinstance(raw, list):
        return jobs
    return {"jobs": jobs, "updated_at": updated_at}


def write_jobs_file(jobs_path: Path, jobs: list[dict[str, Any]]) -> bool:
    """Persist cron jobs to Mongo and jobs.json (Hermes CLI still reads the file)."""
    jobs_path = jobs_path.expanduser()
    payload = _build_jobs_payload(jobs_path, jobs)
    from .runtime_state import save_json

    save_json(_cron_jobs_store_key(jobs_path), payload, jobs_path)
    try:
        jobs_path.parent.mkdir(parents=True, exist_ok=True)
        jobs_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return False
    return True


def remove_cron_jobs_from_file(
    jobs_path: Path,
    *,
    job_ids: Optional[set[str]] = None,
    profiles: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Remove cron rows by id and/or profile directly from jobs.json (always persists)."""
    ids = {str(x).strip() for x in (job_ids or set()) if str(x).strip()}
    profs = {str(x).strip() for x in (profiles or set()) if str(x).strip()}
    if not ids and not profs:
        return {"ok": False, "error": "job_ids or profiles required"}

    jobs, _ = read_jobs_file(jobs_path)
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        jid = str(job.get("id") or "").strip()
        prof = str(job.get("profile") or "").strip()
        drop = (jid and jid in ids) or (prof and prof in profs)
        if drop:
            removed.append(
                {
                    "id": jid,
                    "profile": prof,
                    "name": str(job.get("name") or ""),
                }
            )
            continue
        kept.append(job)

    if not removed:
        return {
            "ok": True,
            "removed_count": 0,
            "removed": [],
            "jobs_path": str(jobs_path),
            "already_absent": True,
        }
    if not write_jobs_file(jobs_path, kept):
        return {"ok": False, "error": f"falha ao gravar {jobs_path}"}
    return {
        "ok": True,
        "removed_count": len(removed),
        "removed": removed,
        "jobs_path": str(jobs_path),
    }


def remove_cron_jobs_for_profile(
    profile: str,
    *,
    jobs_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Delete every cron job bound to a Hermes profile (file-level, always persists)."""
    slug = str(profile or "").strip()
    if not slug:
        return {"ok": False, "error": "profile is required"}
    path = (jobs_path or resolve_hermes_cron_jobs_path()).expanduser()
    return remove_cron_jobs_from_file(path, profiles={slug})


_KANBAN_TASK_CRON_SUFFIX_RE = re.compile(r" — (TASK-\d+)$")


def kanban_task_cron_job_name(assignee: str, task_id: str) -> str:
    return f"{str(assignee or '').strip()} — {str(task_id or '').strip()}"


def _kanban_task_cron_matches(
    job: dict[str, Any],
    task_id: str,
    *,
    assignee: Optional[str] = None,
) -> bool:
    name = str(job.get("name") or "")
    match = _KANBAN_TASK_CRON_SUFFIX_RE.search(name)
    if not match or match.group(1) != str(task_id or "").strip():
        return False
    if assignee:
        prefix = f"{assignee.strip()} — "
        if not name.startswith(prefix):
            return False
    return True


def find_kanban_task_cron_jobs(
    jobs_path: Path,
    task_id: str,
    *,
    assignee: Optional[str] = None,
    preferred_job_id: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    """Return (canonical job, duplicate jobs) for one kanban task id."""
    task_id = str(task_id or "").strip()
    if not task_id:
        return None, []
    jobs, _ = read_jobs_file(jobs_path)
    matches = [
        job
        for job in jobs
        if isinstance(job, dict)
        and _kanban_task_cron_matches(job, task_id, assignee=assignee)
    ]
    if not matches:
        preferred = str(preferred_job_id or "").strip()
        if preferred:
            job = find_job_by_id(preferred, jobs_path)
            if isinstance(job, dict):
                return job, []
        return None, []

    preferred = str(preferred_job_id or "").strip()
    canonical: Optional[dict[str, Any]] = None
    if preferred:
        canonical = next(
            (job for job in matches if str(job.get("id") or "") == preferred),
            None,
        )
    if canonical is None and assignee:
        named = kanban_task_cron_job_name(assignee, task_id)
        canonical = next(
            (job for job in matches if str(job.get("name") or "") == named),
            None,
        )
    if canonical is None and len(matches) == 1:
        canonical = matches[0]
    if canonical is None:
        canonical = sorted(
            matches,
            key=lambda job: str(job.get("next_run_at") or job.get("created_at") or ""),
            reverse=True,
        )[0]

    keep_id = str(canonical.get("id") or "")
    dupes = [job for job in matches if str(job.get("id") or "") != keep_id]
    return canonical, dupes


def prune_kanban_task_cron_duplicates(
    jobs_path: Path,
    task_id: str,
    *,
    keep_job_id: str,
) -> int:
    """Drop extra cron rows that target the same kanban task id."""
    task_id = str(task_id or "").strip()
    keep_job_id = str(keep_job_id or "").strip()
    if not task_id or not keep_job_id:
        return 0
    jobs, _ = read_jobs_file(jobs_path)
    if not jobs:
        return 0
    kept: list[dict[str, Any]] = []
    removed = 0
    for job in jobs:
        if not isinstance(job, dict):
            kept.append(job)
            continue
        jid = str(job.get("id") or "")
        if _kanban_task_cron_matches(job, task_id) and jid != keep_job_id:
            removed += 1
            continue
        kept.append(job)
    if removed and write_jobs_file(jobs_path, kept):
        return removed
    return 0


def _schedule_kind(schedule: Any) -> Optional[str]:
    if isinstance(schedule, dict):
        kind = schedule.get("kind")
        return str(kind) if kind else None
    return None


_DURATION_RE = re.compile(
    r"^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$",
    re.IGNORECASE,
)
_CRON_FIELD_RE = re.compile(r"^[\d\*\-,/]+$")


def normalize_cron_schedule_value(
    schedule: Any,
    *,
    display_hint: str = "",
) -> tuple[Any, bool]:
    """Convert legacy string schedules to Hermes dict shape. Returns (value, changed)."""
    if isinstance(schedule, dict) and schedule.get("kind"):
        return schedule, False
    if not isinstance(schedule, str):
        return schedule, False

    raw = schedule.strip()
    if not raw:
        return schedule, False

    display = (display_hint or raw).strip() or raw
    lower = raw.lower()

    if lower == "once":
        return {"kind": "once", "run_at": None, "display": display or "once"}, True

    if lower.startswith("every "):
        dur = raw[6:].strip()
        m = _DURATION_RE.match(dur)
        if m:
            value = int(m.group(1))
            unit = m.group(2)[0].lower()
            mult = {"m": 1, "h": 60, "d": 1440}[unit]
            minutes = value * mult
            return {
                "kind": "interval",
                "minutes": minutes,
                "display": display or f"every {minutes}m",
            }, True

    parts = raw.split()
    if len(parts) >= 5 and all(_CRON_FIELD_RE.match(p) for p in parts[:5]):
        return {
            "kind": "cron",
            "expr": raw,
            "display": display or raw,
        }, True

    if "T" in raw or re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        return {
            "kind": "once",
            "run_at": raw,
            "display": display or f"once at {raw}",
        }, True

    m = _DURATION_RE.match(raw)
    if m:
        return {
            "kind": "once",
            "run_at": None,
            "display": display or f"once in {raw}",
        }, True

    return schedule, False


CRON_SCHEDULE_PRESETS: dict[str, tuple[str, str]] = {
    "hourly": ("0 * * * *", "De hora em hora"),
    "every_2h": ("0 */2 * * *", "A cada 2 horas"),
    "daily_6": ("0 6 * * *", "Todos os dias às 06:00"),
    "daily_8": ("0 8 * * *", "Todos os dias às 08:00"),
    "daily_9": ("0 9 * * *", "Todos os dias às 09:00"),
    "daily_12": ("0 12 * * *", "Todos os dias às 12:00"),
    "daily_18": ("0 18 * * *", "Todos os dias às 18:00"),
    "daily_20": ("0 20 * * *", "Todos os dias às 20:00"),
    "weekdays_9": ("0 9 * * 1-5", "Seg–sex às 09:00"),
    "weekdays_18": ("0 18 * * 1-5", "Seg–sex às 18:00"),
    "weekly_mon_9": ("0 9 * * 1", "Toda segunda às 09:00"),
}


def cron_schedule_preset_catalog() -> list[dict[str, str]]:
    return [
        {"id": key, "expr": expr, "label": label}
        for key, (expr, label) in CRON_SCHEDULE_PRESETS.items()
    ]


def _interval_minutes_to_parts(minutes: int) -> tuple[int, str]:
    minutes = max(1, int(minutes))
    if minutes % 1440 == 0 and minutes >= 1440:
        return minutes // 1440, "d"
    if minutes % 60 == 0 and minutes >= 60:
        return minutes // 60, "h"
    return minutes, "m"


def _parts_to_interval_minutes(value: int, unit: str) -> int:
    value = max(1, int(value))
    unit = (unit or "m").lower()[:1]
    mult = {"m": 1, "h": 60, "d": 1440}.get(unit, 1)
    return value * mult


def _match_cron_preset(expr: str) -> str:
    needle = (expr or "").strip()
    for key, (preset_expr, _) in CRON_SCHEDULE_PRESETS.items():
        if preset_expr == needle:
            return key
    return "custom"


def cron_schedule_to_builder(schedule: Any) -> dict[str, Any]:
    """Map a stored schedule to dashboard builder fields."""
    if isinstance(schedule, str):
        schedule, _ = normalize_cron_schedule_value(schedule)

    if not isinstance(schedule, dict):
        return {
            "builder_kind": "interval",
            "interval_value": 60,
            "interval_unit": "m",
            "interval_minutes": 60,
            "cron_preset": "hourly",
            "cron_expr": "0 * * * *",
            "schedule_string": "every 60m",
        }

    kind = schedule.get("kind") or "interval"
    if kind == "interval":
        minutes = int(schedule.get("minutes") or 60)
        value, unit = _interval_minutes_to_parts(minutes)
        if minutes >= 60 and minutes % 60 == 0 and minutes < 1440:
            sched_str = f"every {minutes // 60}h"
        elif minutes >= 1440 and minutes % 1440 == 0:
            sched_str = f"every {minutes // 1440}d"
        else:
            sched_str = f"every {minutes}m"
        return {
            "builder_kind": "interval",
            "interval_value": value,
            "interval_unit": unit,
            "interval_minutes": minutes,
            "cron_preset": "hourly",
            "cron_expr": "0 * * * *",
            "schedule_string": sched_str,
        }

    if kind == "cron":
        expr = str(schedule.get("expr") or "0 * * * *").strip()
        return {
            "builder_kind": "cron",
            "interval_value": 60,
            "interval_unit": "m",
            "interval_minutes": 60,
            "cron_preset": _match_cron_preset(expr),
            "cron_expr": expr,
            "schedule_string": expr,
        }

    if kind == "once":
        run_at = schedule.get("run_at")
        once_str = f"once at {run_at}" if run_at else "once"
        return {
            "builder_kind": "once",
            "interval_value": 1,
            "interval_unit": "m",
            "interval_minutes": 1,
            "cron_preset": "custom",
            "cron_expr": "",
            "schedule_string": once_str,
        }

    return cron_schedule_to_builder(None)


def compose_cron_schedule_from_builder(payload: dict[str, Any]) -> str:
    """Build a Hermes CLI ``--schedule`` string from dashboard builder fields."""
    kind = str(payload.get("builder_kind") or payload.get("sched_kind") or "interval").strip()

    if kind == "interval":
        minutes = payload.get("interval_minutes")
        if minutes is None:
            minutes = _parts_to_interval_minutes(
                int(payload.get("interval_value") or 60),
                str(payload.get("interval_unit") or "m"),
            )
        minutes = max(1, int(minutes))
        if minutes >= 1440 and minutes % 1440 == 0:
            days = minutes // 1440
            return f"every {days}d"
        if minutes >= 60 and minutes % 60 == 0:
            hours = minutes // 60
            return f"every {hours}h"
        return f"every {minutes}m"

    if kind == "cron":
        preset = str(payload.get("cron_preset") or "").strip()
        if preset and preset != "custom" and preset in CRON_SCHEDULE_PRESETS:
            return CRON_SCHEDULE_PRESETS[preset][0]
        expr = str(payload.get("cron_expr") or payload.get("schedule") or "").strip()
        if not expr:
            raise ValueError("informe uma expressão cron (5 campos) ou escolha um preset")
        parts = expr.split()
        if len(parts) < 5:
            raise ValueError("expressão cron inválida — use 5 campos, ex.: 0 9 * * *")
        return expr

    if kind == "once":
        run_at = str(payload.get("run_at") or "").strip()
        if run_at:
            return run_at
        return "once"

    raise ValueError(f"tipo de periodicidade desconhecido: {kind!r}")


def resolve_edit_schedule_payload(payload: dict[str, Any]) -> str:
    """Accept raw ``schedule`` or structured builder fields from the dashboard."""
    if any(
        key in payload
        for key in (
            "builder_kind",
            "sched_kind",
            "interval_minutes",
            "interval_value",
            "cron_preset",
            "cron_expr",
        )
    ):
        return compose_cron_schedule_from_builder(payload)
    schedule = payload.get("schedule")
    if schedule is None:
        raise ValueError("schedule is required")
    if not isinstance(schedule, str):
        raise ValueError("schedule must be a string")
    text = schedule.strip()
    if not text:
        raise ValueError("schedule cannot be empty")
    return text


def repair_malformed_cron_schedules(
    jobs_path: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fix jobs whose ``schedule`` is a bare string (breaks Hermes ``cron tick``)."""
    jobs_path = jobs_path.expanduser().resolve()
    if not jobs_path.is_file():
        return {"ok": False, "error": f"jobs file not found: {jobs_path}", "repaired": 0}

    try:
        raw_text = jobs_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc), "repaired": 0}

    if isinstance(data, list):
        wrapper: dict[str, Any] = {"jobs": data}
    elif isinstance(data, dict):
        wrapper = data
    else:
        return {"ok": False, "error": "unexpected jobs.json shape", "repaired": 0}

    jobs = wrapper.get("jobs")
    if not isinstance(jobs, list):
        return {"ok": False, "error": "jobs.json has no jobs list", "repaired": 0}

    repaired: list[dict[str, str]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        schedule = job.get("schedule")
        if not isinstance(schedule, str):
            continue
        display_hint = str(job.get("schedule_display") or schedule).strip()
        new_schedule, changed = normalize_cron_schedule_value(
            schedule,
            display_hint=display_hint,
        )
        if not changed:
            continue
        job["schedule"] = new_schedule
        if display_hint and not job.get("schedule_display"):
            job["schedule_display"] = new_schedule.get("display") or display_hint
        repaired.append(
            {
                "id": str(job.get("id") or ""),
                "name": str(job.get("name") or ""),
                "before": schedule,
                "after": str(new_schedule.get("display") or new_schedule.get("kind")),
            }
        )

    if not repaired:
        return {"ok": True, "repaired": 0, "jobs_path": str(jobs_path)}

    if dry_run:
        return {
            "ok": True,
            "repaired": len(repaired),
            "dry_run": True,
            "jobs": repaired,
            "jobs_path": str(jobs_path),
        }

    wrapper["updated_at"] = datetime.now().astimezone().isoformat()
    backup = jobs_path.with_suffix(jobs_path.suffix + ".bak-schedule-repair")
    backup.write_text(raw_text, encoding="utf-8")
    jobs_path.write_text(
        json.dumps(wrapper, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "ok": True,
        "repaired": len(repaired),
        "jobs": repaired,
        "backup": str(backup),
        "jobs_path": str(jobs_path),
    }


def cron_output_dir_for_jobs_path(jobs_path: Path) -> Path:
    """Hermes writes run artifacts next to jobs.json under cron/output/."""
    return jobs_path.expanduser().parent / "output"


def resolve_cron_output_dir(job_id: str, output_root: Path) -> Optional[Path]:
    """Return ~/.hermes/cron/output/{job_id} when it exists."""
    root = output_root.expanduser()
    if not root.is_dir():
        return None
    job_id = str(job_id)
    exact = root / job_id
    if exact.is_dir():
        return exact
    prefix = job_id.split("-", 1)[0]
    if prefix != job_id:
        for child in root.iterdir():
            if child.is_dir() and child.name.startswith(prefix):
                return child
    return None


def _strip_markdown_preview(text: str, max_len: int = 160) -> str:
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    cleaned = re.sub(r"[*_#>`|]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1].rstrip() + "…"


_RUN_FILE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})\.md$")

# Top-level sections in Hermes cron output markdown. Agent responses often
# contain nested ``## …`` headings — do not truncate there.
_CRON_OUTPUT_SECTIONS = ("## Prompt", "## Response", "## Error", "## Files")


def _cron_markdown_section(text: str, marker: str) -> str:
    """Return body of one Hermes cron output section until the next known section."""
    idx = text.find(marker)
    if idx < 0:
        return ""
    chunk = text[idx + len(marker) :].strip()
    next_pos = len(chunk)
    for other in _CRON_OUTPUT_SECTIONS:
        if other == marker:
            continue
        pos = chunk.find("\n" + other)
        if pos >= 0:
            next_pos = min(next_pos, pos)
    return chunk[:next_pos].strip()


def parse_cron_output_markdown(text: str) -> dict[str, Any]:
    """Extract metadata, agent response, and error from a Hermes cron output file."""
    run_time: Optional[str] = None
    for line in text.splitlines()[:12]:
        if line.startswith("**Run Time:**"):
            run_time = line.split(":", 1)[1].strip().strip("*").strip()
            break

    def _section(marker: str) -> str:
        return _cron_markdown_section(text, marker)

    response = _section("## Response")
    error_raw = _section("## Error")
    error = error_raw.strip("`\n ") if error_raw else ""

    if not response and error:
        response = error
    elif not response and text.strip():
        response = text.strip()

    preview_source = error or response
    preview = _strip_markdown_preview(preview_source) if preview_source else ""
    return {
        "run_time": run_time,
        "response": response,
        "error": error or None,
        "preview": preview,
    }


_BACKTICK_PATH_RE = re.compile(r"`([^`\n]+)`")
_FILES_SECTION_RE = re.compile(r"^##\s+Files\s*\n", re.MULTILINE | re.IGNORECASE)
_BULLET_PATH_RE = re.compile(
    r"^\s*[-*]\s+(?:`([^`]+)`|([\w./][^\s`]*))\s*$",
    re.MULTILINE,
)


def extract_paths_from_agent_response(text: str) -> list[str]:
    """Pull file paths from backticks, bullets, and a ## Files section."""
    if not text or not text.strip():
        return []

    seen: set[str] = set()
    paths: list[str] = []

    def _add(raw: str) -> None:
        path = raw.strip().strip("'\"")
        if not path or path in seen:
            return
        if "://" in path:
            return
        seen.add(path)
        paths.append(path)

    for match in _BACKTICK_PATH_RE.finditer(text):
        _add(match.group(1))

    files_match = _FILES_SECTION_RE.search(text)
    if files_match:
        rest = text[files_match.end() :]
        section = rest.split("\n##", 1)[0]
        for line in section.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(("-", "*")):
                item = stripped.lstrip("-*").strip()
                if item.startswith("`") and item.endswith("`"):
                    _add(item[1:-1])
                else:
                    _add(item.split()[0] if item.split() else item)
            elif stripped.startswith("`") and stripped.endswith("`"):
                _add(stripped[1:-1])
            else:
                _add(stripped)

    for match in _BULLET_PATH_RE.finditer(text):
        _add(match.group(1) or match.group(2) or "")

    return paths


def workdir_search_roots(
    *,
    extra: Optional[list[str | Path]] = None,
    create_cfg: Any = None,
) -> list[Path]:
    """Candidate directories when inferring cron/swarm workdir."""
    from .local_paths import project_stack_root

    roots: list[Path] = []
    seen: set[str] = set()

    def _add(raw: str | Path) -> None:
        try:
            p = Path(raw).expanduser().resolve()
            key = str(p)
            if key in seen or not p.is_dir():
                return
            seen.add(key)
            roots.append(p)
        except OSError:
            return

    _add(Path.cwd())
    try:
        from .local_paths import _repo_root

        _add(_repo_root())
    except Exception:
        pass
    if create_cfg is not None:
        default_wd = str(getattr(create_cfg, "default_workdir", None) or "").strip()
        if default_wd:
            _add(default_wd)
        projects_root = str(getattr(create_cfg, "projects_root", None) or "").strip()
        if projects_root:
            _add(projects_root)
    _add(project_stack_root())
    for item in extra or []:
        _add(item)
    return roots


def infer_workdir_from_paths(
    paths: list[str],
    *,
    search_roots: Optional[list[Path]] = None,
    create_cfg: Any = None,
) -> str:
    """Pick a repo root that contains files mentioned in an agent response."""
    if not paths:
        return ""
    roots = search_roots or workdir_search_roots(create_cfg=create_cfg)
    if not roots:
        return ""

    matched: list[Path] = []
    for path_str in paths:
        raw = str(path_str or "").strip().strip('"').strip("'")
        if not raw or "://" in raw:
            continue
        rel = Path(raw)
        if rel.is_absolute():
            if rel.is_file():
                for root in roots:
                    try:
                        rel.relative_to(root)
                        matched.append(root)
                        break
                    except ValueError:
                        continue
                else:
                    matched.append(rel.parent.resolve())
            continue

        found = False
        for root in roots:
            candidate = root / rel
            if candidate.is_file():
                matched.append(root)
                found = True
                break
        if found:
            continue
        parts = rel.parts
        if not parts:
            continue
        for root in roots:
            nested = root / parts[0]
            tail = Path(*parts[1:]) if len(parts) > 1 else Path()
            if nested.is_dir() and (nested / tail).is_file():
                matched.append(root)
                break

    if not matched:
        return ""
    counts: dict[str, int] = {}
    for root in matched:
        key = str(root)
        counts[key] = counts.get(key, 0) + 1
    best = max(counts, key=lambda k: (counts[k], -len(k)))
    return best


def infer_workdir_for_swarm_profile(
    profile_slug: str,
    *,
    create_cfg: Any = None,
) -> str:
    """Resolve workdir from profile swarm_name and on-disk project folders."""
    from .hermes_profile_model import read_profile_meta_dict

    slug = str(profile_slug or "").strip()
    if not slug:
        return ""

    swarm_name = str(read_profile_meta_dict(slug).get("swarm_name") or "").strip()
    candidates: list[str] = []
    if swarm_name:
        candidates.append(swarm_name)
    parts = slug.split("-")
    for end in range(len(parts), 0, -1):
        prefix = "-".join(parts[:end])
        if prefix and prefix not in candidates:
            candidates.append(prefix)

    for root in workdir_search_roots(create_cfg=create_cfg):
        for name in candidates:
            nested = root / name
            if nested.is_dir():
                return str(root.resolve())
        if root.name in candidates:
            return str(root.resolve())
    return ""


def resolve_cron_workdir(
    job: Optional[dict[str, Any]],
    response: str = "",
    *,
    profile_slug: str = "",
    create_cfg: Any = None,
) -> str:
    """Effective workdir for a cron run (explicit job field, paths, or swarm folder)."""
    wd = str((job or {}).get("workdir") or "").strip()
    if wd:
        return wd

    slug = str(profile_slug or (job or {}).get("profile") or "").strip()
    paths = extract_paths_from_agent_response(response)
    inferred = infer_workdir_from_paths(paths, create_cfg=create_cfg)
    if inferred:
        return inferred
    return infer_workdir_for_swarm_profile(slug, create_cfg=create_cfg)


def read_git_file_diff(
    workdir: str | Path,
    rel_path: str,
    *,
    max_chars: int = 120_000,
) -> str:
    """Return unified diff for *rel_path* under *workdir* (tracked or untracked)."""
    wd = Path(workdir).expanduser()
    path = str(rel_path or "").strip().strip('"').strip("'")
    if not wd.is_dir() or not path:
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(wd), "diff", "HEAD", "--", path],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            text = proc.stdout
            return text[:max_chars] + ("\n\n… (truncado)" if len(text) > max_chars else "")
        candidate = (wd / path).resolve()
        if candidate.is_file():
            proc2 = subprocess.run(
                ["git", "-C", str(wd), "diff", "--no-index", "--", "/dev/null", path],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if proc2.stdout.strip():
                text = proc2.stdout
                return text[:max_chars] + ("\n\n… (truncado)" if len(text) > max_chars else "")
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return ""


def list_git_workdir_files(workdir: str | Path) -> list[dict[str, Any]]:
    """Return changed paths from ``git status --porcelain`` in *workdir*."""
    wd = Path(workdir).expanduser()
    if not wd.is_dir():
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", str(wd), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []

    files: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if xy == "??":
            status = "untracked"
        elif "A" in xy:
            status = "added"
        elif "D" in xy:
            status = "deleted"
        elif "M" in xy:
            status = "modified"
        else:
            status = "modified"
        files.append({"path": path, "status": status, "source": "git"})
    return files


def collect_generated_files(
    response: str,
    workdir: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Merge git workdir changes with paths mentioned in the agent response."""
    by_path: dict[str, dict[str, Any]] = {}
    if workdir:
        for item in list_git_workdir_files(workdir):
            by_path[item["path"]] = dict(item)
    for path in extract_paths_from_agent_response(response):
        if path in by_path:
            continue
        by_path[path] = {"path": path, "status": "mentioned", "source": "response"}
    return sorted(by_path.values(), key=lambda row: row.get("path") or "")


def list_cron_job_runs(
    job_id: str,
    output_root: Path,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List saved run files for one job, newest first."""
    job_dir = resolve_cron_output_dir(job_id, output_root)
    if job_dir is None:
        return []

    runs: list[dict[str, Any]] = []
    for path in sorted(job_dir.glob("*.md"), reverse=True):
        if len(runs) >= limit:
            break
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        parsed = parse_cron_output_markdown(text)
        runs.append(
            {
                "file": path.name,
                "run_time": parsed.get("run_time"),
                "preview": parsed.get("preview") or "",
            }
        )
    return runs


def _parse_run_anchor(
    run_time: Optional[str],
    run_file: Optional[str],
) -> Optional[datetime]:
    if run_file:
        match = _RUN_FILE_TS_RE.match(Path(run_file).name)
        if match:
            try:
                return datetime.strptime(
                    f"{match.group(1)} {match.group(2).replace('-', ':')}",
                    "%Y-%m-%d %H:%M:%S",
                )
            except ValueError:
                pass
    if run_time:
        raw = str(run_time).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw[:19], fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return None


def _cron_session_id(job_id: str, run_file: Optional[str]) -> Optional[str]:
    if not run_file:
        return None
    match = _RUN_FILE_TS_RE.match(Path(run_file).name)
    if not match:
        return None
    prefix = str(job_id).split("-", 1)[0]
    stamp = f"{match.group(1).replace('-', '')}_{match.group(2).replace('-', '')}"
    return f"cron_{prefix}_{stamp}"


def _log_line_timestamp(line: str) -> Optional[datetime]:
    match = _TS_RE.match(line)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def extract_cron_execution_log(
    job_id: str,
    *,
    run_time: Optional[str] = None,
    run_file: Optional[str] = None,
    job_name: Optional[str] = None,
    agent_log_path: Optional[Path] = None,
    errors_log_path: Optional[Path] = None,
    max_lines: int = 120,
    window_seconds: int = 300,
) -> list[str]:
    """Collect agent/errors log lines for one cron run."""
    anchor = _parse_run_anchor(run_time, run_file)
    job_id = str(job_id)
    prefix = job_id.split("-", 1)[0]
    session_id = _cron_session_id(job_id, run_file)

    if session_id and agent_log_path and agent_log_path.is_file():
        lines = _read_log_tail(agent_log_path, 15000)
        block: list[str] = []
        for line in lines:
            if session_id not in line:
                if block:
                    break
                continue
            block.append(line.rstrip())
            if "Turn ended:" in line:
                break
            if len(block) >= max_lines:
                break
        if block:
            return block[:max_lines]

    collected: list[str] = []
    seen: set[str] = set()

    def _add(line: str) -> None:
        stripped = line.rstrip()
        if not stripped or stripped in seen:
            return
        seen.add(stripped)
        collected.append(stripped)

    def _in_window(line: str) -> bool:
        if anchor is None:
            return True
        ts = _log_line_timestamp(line)
        if ts is None:
            return bool(collected)
        return abs((ts - anchor).total_seconds()) <= window_seconds

    def _matches(line: str) -> bool:
        if job_id in line or prefix in line:
            return True
        if job_name and job_name in line:
            return True
        if session_id and session_id in line:
            return True
        return False

    for path in (agent_log_path, errors_log_path):
        if path is None or not path.is_file():
            continue
        for line in _read_log_tail(path, 15000):
            if not _matches(line) or not _in_window(line):
                continue
            _add(line)
            if len(collected) >= max_lines:
                break
        if len(collected) >= max_lines:
            break

    return collected[:max_lines]


def _run_matches_job_last_run(run_time: Optional[str], job: dict[str, Any]) -> bool:
    last_run = job.get("last_run_at")
    if not run_time or not last_run:
        return False
    anchor = _parse_run_anchor(run_time, None)
    last_dt: Optional[datetime]
    try:
        last_dt = datetime.fromisoformat(str(last_run))
    except ValueError:
        last_dt = _parse_run_anchor(str(last_run), None)
    if anchor is None or last_dt is None:
        return False
    if hasattr(last_dt, "tzinfo") and last_dt.tzinfo is not None:
        last_dt = last_dt.replace(tzinfo=None)
    return abs((anchor - last_dt).total_seconds()) <= 120


def get_cron_job_result(
    job_id: str,
    output_root: Path,
    *,
    run_file: Optional[str] = None,
    max_response_chars: int = 50000,
    workdir: Optional[str] = None,
    job: Optional[dict[str, Any]] = None,
    agent_log_path: Optional[Path] = None,
    errors_log_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Load one saved cron run (latest by default) from cron/output/."""
    job_dir = resolve_cron_output_dir(job_id, output_root)
    if job_dir is None:
        job_label = str((job or {}).get("name") or "").strip()
        hint = f"job {job_label!r} ainda não possui execuções salvas" if job_label else "job ainda não possui execuções salvas"
        return {
            "ok": False,
            "error": f"no saved runs for this job ({hint})",
            "job_id": job_id,
        }

    if run_file:
        if Path(run_file).name != run_file or ".." in run_file:
            return {"ok": False, "error": "invalid run file name", "job_id": job_id}
        path = job_dir / run_file
        if not path.is_file():
            return {"ok": False, "error": f"run file {run_file!r} not found", "job_id": job_id}
    else:
        candidates = sorted(job_dir.glob("*.md"), reverse=True)
        if not candidates:
            return {
                "ok": False,
                "error": "no saved runs for this job",
                "job_id": job_id,
            }
        path = candidates[0]

    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return {
            "ok": False,
            "error": f"cannot read {path.name}: {exc}",
            "job_id": job_id,
        }

    parsed = parse_cron_output_markdown(text)
    response = parsed.get("response") or ""
    error = parsed.get("error") or ""
    if not error and job and job.get("last_error") and _run_matches_job_last_run(
        parsed.get("run_time"), job
    ):
        error = str(job.get("last_error") or "")
    if len(response) > max_response_chars:
        response = response[:max_response_chars] + "\n\n… (truncated)"

    runs = list_cron_job_runs(job_id, output_root, limit=10)
    effective_workdir = resolve_cron_workdir(
        job,
        response,
        profile_slug=str((job or {}).get("profile") or ""),
    )
    if not effective_workdir and workdir:
        effective_workdir = str(workdir).strip()
    generated_files = collect_generated_files(
        response,
        effective_workdir or (str(workdir).strip() if workdir else None),
    )
    execution_log = extract_cron_execution_log(
        job_id,
        run_time=parsed.get("run_time"),
        run_file=path.name,
        job_name=str((job or {}).get("name") or "") or None,
        agent_log_path=agent_log_path,
        errors_log_path=errors_log_path,
    )
    status = job.get("last_status") if job else None
    if error:
        status = "error"
    elif status is None:
        status = _infer_last_status_from_preview(parsed.get("preview") or response)
    result: dict[str, Any] = {
        "ok": True,
        "job_id": job_id,
        "file": path.name,
        "run_time": parsed.get("run_time"),
        "preview": parsed.get("preview") or "",
        "response": response,
        "error": error or None,
        "last_error": (job or {}).get("last_error"),
        "last_status": status,
        "execution_log": execution_log,
        "runs": runs,
        "generated_files": generated_files,
    }
    resolved_workdir = str(effective_workdir or workdir or "").strip()
    if resolved_workdir:
        result["workdir"] = str(Path(resolved_workdir).expanduser())
    return result


def _cron_run_time_to_iso(run_time: str) -> str:
    """Normalize Hermes cron output ``Run Time`` for dashboard Date parsing."""
    s = str(run_time).strip()
    if not s:
        return s
    if "T" in s:
        return s
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").isoformat()
    except ValueError:
        return s


def _infer_last_status_from_preview(preview: str) -> str:
    """Best-effort status when jobs.json omits last_status but output exists."""
    text = (preview or "").strip()
    if not text:
        return "ok"
    low = text.lower()
    if "skipped" in low or "concurrency at max" in low or "⏭" in text:
        return "skipped"
    if "error" in low and ("failed" in low or "falha" in low):
        return "error"
    return "ok"


def _cron_job_skills(job: dict[str, Any]) -> list[str]:
    """Normalize Hermes cron skill fields to a deduplicated list."""
    raw = job.get("skills")
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = [str(item) for item in raw]
    else:
        values = []
    single = str(job.get("skill") or "").strip()
    if single:
        values.append(single)

    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def summarize_cron_job(
    job: dict[str, Any],
    *,
    output_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Compact job record safe for /status JSON and the dashboard."""
    schedule = job.get("schedule")
    enabled = bool(job.get("enabled"))
    state = job.get("state") or ""
    summary: dict[str, Any] = {
        "id": job.get("id"),
        "name": job.get("name") or job.get("id") or "?",
        "prompt": str(job.get("prompt") or ""),
        "schedule_display": job.get("schedule_display")
        or (schedule.get("display") if isinstance(schedule, dict) else None)
        or (schedule.get("expr") if isinstance(schedule, dict) else None),
        "schedule_kind": _schedule_kind(schedule),
        "enabled": enabled,
        "state": state,
        "active": enabled and state == "scheduled" and bool(schedule),
        "next_run_at": job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "last_error": job.get("last_error"),
        "profile": job.get("profile"),
        "skills": _cron_job_skills(job),
        "last_result_preview": None,
        "last_result_file": None,
        "running": False,
    }

    job_id = job.get("id")
    if output_root is not None and job_id is not None:
        runs = list_cron_job_runs(str(job_id), output_root, limit=1)
        if runs:
            latest = runs[0]
            summary["last_result_preview"] = latest.get("preview") or None
            summary["last_result_file"] = latest.get("file")
            run_time = latest.get("run_time")
            if run_time:
                summary["last_result_at"] = run_time
                if not summary["last_run_at"]:
                    summary["last_run_at"] = _cron_run_time_to_iso(str(run_time))
            if summary["last_status"] is None:
                if job.get("last_error"):
                    summary["last_status"] = "error"
                else:
                    summary["last_status"] = _infer_last_status_from_preview(
                        summary["last_result_preview"] or "",
                    )

    wa = cron_job_whatsapp_delivery(job)
    summary["whatsapp_sends"] = wa["whatsapp_sends"]
    summary["whatsapp_to"] = wa["whatsapp_to"]
    summary["whatsapp_sources"] = wa.get("whatsapp_sources") or []

    return summary


def resolve_agent_log_path(log_paths: Optional[list[str]] = None) -> Optional[Path]:
    """Pick ~/.hermes/logs/agent.log from configured Hermes log_paths."""
    for raw in log_paths or []:
        path = Path(raw).expanduser()
        if path.name == "agent.log":
            return path
    fallback = hermes_home() / "logs" / "agent.log"
    return fallback if fallback.is_file() else None


def _read_log_tail(path: Path, max_lines: int) -> list[str]:
    if not path.is_file():
        return []
    try:
        with path.open("r", errors="replace") as handle:
            return list(deque(handle, maxlen=max_lines))
    except OSError:
        return []


def _session_started_at(session_id: str) -> Optional[str]:
    match = _SESSION_STARTED_AT_RE.search(session_id)
    if not match:
        return None
    day, clock = match.group(1), match.group(2)
    try:
        dt = datetime.strptime(f"{day}{clock}", "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return dt.isoformat(sep=" ")


@dataclass
class _CronSessionState:
    session_id: str
    job_id: str
    started: bool = False
    ended: bool = False
    last_ts: Optional[datetime] = None
    last_message: str = ""
    log_lines: list[str] = field(default_factory=list)


def detect_running_cron_jobs(
    agent_log_path: Path,
    jobs_path: Path,
    *,
    max_log_lines: int = 10000,
    log_tail_lines: int = 12,
    stale_seconds: float = 900.0,
) -> list[dict[str, Any]]:
    """Find Hermes cron sessions still running by scanning agent.log."""
    lines = _read_log_tail(agent_log_path, max_log_lines)
    if not lines:
        return []

    sessions: dict[str, _CronSessionState] = {}
    for line in lines:
        match = _CRON_SESSION_RE.search(line)
        if not match:
            continue
        session_id = match.group(1)
        state = sessions.get(session_id)
        if state is None:
            job_match = _JOB_ID_FROM_SESSION_RE.match(session_id)
            state = _CronSessionState(
                session_id=session_id,
                job_id=job_match.group(1) if job_match else session_id,
            )
            sessions[session_id] = state

        ts_match = _TS_RE.match(line)
        if ts_match:
            try:
                state.last_ts = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

        message = line.split("] ", 1)[-1] if "] " in line else line
        state.last_message = message[:300]
        state.log_lines.append(message)
        if len(state.log_lines) > log_tail_lines:
            state.log_lines.pop(0)

        if "conversation turn: session=" in line:
            state.started = True
        if "Turn ended:" in line:
            state.ended = True

    jobs, _ = read_jobs_file(jobs_path)
    jobs_by_id = {str(job.get("id")): job for job in jobs if job.get("id") is not None}

    now = datetime.now()
    running: list[dict[str, Any]] = []
    for state in sessions.values():
        if not state.started or state.ended or state.last_ts is None:
            continue
        age = (now - state.last_ts).total_seconds()
        if age > stale_seconds:
            continue

        job = jobs_by_id.get(state.job_id)
        # Ignore orphan sessions that no longer have a persisted cron job.
        # This prevents stale IDs from reappearing in the dashboard.
        if not job:
            continue
        job_state = str(job.get("state") or "").strip().lower()
        if job.get("enabled") is False or job_state == "paused":
            continue
        running.append(
            {
                "session_id": state.session_id,
                "job_id": state.job_id,
                "name": job.get("name") or state.job_id,
                "profile": job.get("profile"),
                "workdir": job.get("workdir"),
                "started_at": _session_started_at(state.session_id),
                "last_activity_at": state.last_ts.isoformat(sep=" "),
                "seconds_since_activity": int(age),
                "last_message": state.last_message,
                "log_tail": list(state.log_lines),
            }
        )

    running.sort(key=lambda item: item.get("last_activity_at") or "", reverse=True)
    return running


# Token-usage hints emitted by Hermes (e.g. stale-stream warnings carry the
# current request context size as ``model=<id> context=~<N> tokens``).
_CRON_TOKEN_USAGE_RE = re.compile(
    r"model=(?P<model>\S+)\s+context=~(?P<tok>[\d,]+)\s+tokens",
    re.IGNORECASE,
)

# Newer API-call log format:
# ``API call #N: model=<id> provider=<p> in=<N> out=<N> total=<N>``
_CRON_API_CALL_TOKEN_RE = re.compile(
    r"API call #\d+:\s+model=(?P<model>\S+)\s+.*?\btotal=(?P<tok>[\d,]+)",
    re.IGNORECASE,
)


def compute_cron_token_stats(
    agent_log_path: Optional[Path],
    *,
    max_log_lines: int = 200000,
) -> dict[str, dict[str, Any]]:
    """Average per-run token context per cron job, scanned from ``agent.log``.

    Returns a mapping ``job_id -> {avg_tokens, max_tokens, runs, model}``.
    Multiple log lines for the same Hermes session count as ONE run (we keep
    the max ``context=~N`` seen for that session, which best reflects how
    large the request grew before completion). The result is the arithmetic
    mean of those per-run maxima across all observed runs of the job.
    """
    if agent_log_path is None:
        return {}
    lines = _read_log_tail(agent_log_path, max_log_lines)
    if not lines:
        return {}

    # session_id -> (job_id, max_tokens, model)
    per_session: dict[str, tuple[str, int, Optional[str]]] = {}
    for line in lines:
        session_match = _CRON_SESSION_RE.search(line)
        if not session_match:
            continue
        token_match = _CRON_API_CALL_TOKEN_RE.search(line) or _CRON_TOKEN_USAGE_RE.search(line)
        if not token_match:
            continue
        try:
            tokens = int(token_match.group("tok").replace(",", ""))
        except ValueError:
            continue
        if tokens <= 0:
            continue
        session_id = session_match.group(1)
        job_match = _JOB_ID_FROM_SESSION_RE.match(session_id)
        job_id = job_match.group(1) if job_match else session_id
        model = token_match.group("model") or None
        prev = per_session.get(session_id)
        if prev is None or tokens > prev[1]:
            per_session[session_id] = (job_id, tokens, model)

    agg: dict[str, dict[str, Any]] = {}
    for job_id, tokens, model in per_session.values():
        entry = agg.setdefault(
            job_id,
            {"total": 0, "runs": 0, "max_tokens": 0, "model": None},
        )
        entry["total"] += tokens
        entry["runs"] += 1
        if tokens > entry["max_tokens"]:
            entry["max_tokens"] = tokens
        if model:
            entry["model"] = model

    out: dict[str, dict[str, Any]] = {}
    for job_id, entry in agg.items():
        runs = entry["runs"] or 1
        out[job_id] = {
            "avg_tokens": int(round(entry["total"] / runs)),
            "max_tokens": entry["max_tokens"],
            "runs": entry["runs"],
            "model": entry["model"],
        }
    return out


_TRANSIENT_CRON_SCHEDULER_ERRORS = (
    "cannot schedule new futures after interpreter shutdown",
    "interpreter shutdown",
)


def is_transient_cron_scheduler_error(message: str) -> bool:
    """True when a cron failure was caused by gateway/process shutdown mid-run."""
    text = (message or "").lower()
    return any(fragment in text for fragment in _TRANSIENT_CRON_SCHEDULER_ERRORS)


def cron_tick_lock_path(jobs_path: Path) -> Path:
    return hermes_home_from_jobs_path(jobs_path) / "cron" / ".tick.lock"


def is_cron_tick_in_progress(jobs_path: Path) -> bool:
    """Return True when the Hermes gateway holds the cron tick file lock."""
    lock_file = cron_tick_lock_path(jobs_path)
    if not lock_file.is_file():
        return False
    try:
        import fcntl
    except ImportError:
        try:
            return (time.time() - lock_file.stat().st_mtime) < 600.0
        except OSError:
            return False
    try:
        with open(lock_file, "a+", encoding="utf-8") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except (OSError, BlockingIOError):
        return True


def cron_gateway_restart_blockers(
    jobs_path: Path,
    agent_log_path: Path,
    *,
    stale_seconds: float = 120.0,
) -> list[str]:
    """Reasons to defer ``hermes gateway stop/start`` while cron work is active."""
    blockers: list[str] = []
    if is_cron_tick_in_progress(jobs_path):
        blockers.append("cron tick em progresso (.tick.lock)")
    running = detect_running_cron_jobs(
        agent_log_path,
        jobs_path,
        stale_seconds=stale_seconds,
    )
    if running:
        names = ", ".join(
            str(r.get("name") or r.get("job_id") or "?") for r in running[:3]
        )
        suffix = f" (+{len(running) - 3} mais)" if len(running) > 3 else ""
        blockers.append(f"{len(running)} cron job(s) em execução ({names}{suffix})")
    return blockers


def wait_cron_tick_idle(
    jobs_path: Path,
    *,
    timeout_seconds: float = 90.0,
    poll_seconds: float = 2.0,
) -> bool:
    """Wait until the cron tick lock is free (best-effort)."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not is_cron_tick_in_progress(jobs_path):
            return True
        time.sleep(poll_seconds)
    return not is_cron_tick_in_progress(jobs_path)


def hermes_cron_snapshot(
    jobs_path: Path,
    *,
    agent_log_path: Optional[Path] = None,
    output_root: Optional[Path] = None,
    running_log_tail_lines: int = 12,
) -> dict[str, Any]:
    """Aggregate cron jobs with schedule for gois /status."""
    jobs, updated_at = read_jobs_file(jobs_path)
    with_schedule = [j for j in jobs if j.get("schedule")]
    if output_root is None:
        output_root = cron_output_dir_for_jobs_path(jobs_path)
    summaries = [
        summarize_cron_job(j, output_root=output_root) for j in with_schedule
    ]
    # Defensive de-duplication: malformed/hand-edited jobs stores can contain
    # repeated records for the same id. Keep one row per id in snapshots.
    deduped: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        sid = str(summary.get("id") or "").strip()
        if sid:
            deduped[sid] = summary
            continue
        # If id is missing, keep a synthetic key so we still show the row.
        deduped[f"__row__{len(deduped)}"] = summary
    summaries = list(deduped.values())
    active = [s for s in summaries if s["active"]]
    paused = [s for s in summaries if not s["active"]]
    errors = [s for s in active if s.get("last_status") == "error"]

    def sort_key(s: dict[str, Any]) -> tuple:
        return (s.get("next_run_at") or "z", s.get("name") or "")

    ordered = sorted(active, key=sort_key) + sorted(paused, key=sort_key)
    running: list[dict[str, Any]] = []
    if agent_log_path is not None:
        running = detect_running_cron_jobs(
            agent_log_path,
            jobs_path,
            log_tail_lines=running_log_tail_lines,
        )

    running_ids = {str(item.get("job_id")) for item in running if item.get("job_id")}
    for summary in summaries:
        if str(summary.get("id")) in running_ids:
            summary["running"] = True

    token_stats = (
        compute_cron_token_stats(agent_log_path)
        if agent_log_path is not None
        else {}
    )
    if token_stats:
        for summary in summaries:
            stats = token_stats.get(str(summary.get("id") or ""))
            if stats:
                summary["avg_tokens"] = stats["avg_tokens"]
                summary["token_runs"] = stats["runs"]
                summary["token_max"] = stats["max_tokens"]
                if stats.get("model"):
                    summary["token_model"] = stats["model"]

    return {
        "jobs_path": str(jobs_path.expanduser()),
        "updated_at": updated_at,
        "total": len(summaries),
        "active_count": len(active),
        "paused_count": len(paused),
        "error_count": len(errors),
        "running_count": len(running),
        "running": running,
        "jobs": ordered,
        "ok": True,
    }


def _cron_row_matches_filter(
    row: dict[str, Any],
    *,
    job_id: str,
    query: str,
) -> bool:
    jid = str(row.get("id") or row.get("job_id") or "")
    if job_id:
        needle = job_id
        id_ok = (
            jid == needle
            or (jid and jid.startswith(needle))
            or (jid and needle in jid)
            or (jid and needle.endswith(jid))
            or (jid and jid.endswith(needle))
        )
        if not id_ok:
            return False
    if query:
        hay = " ".join(
            [
                jid,
                str(row.get("name") or ""),
                str(row.get("profile") or ""),
                str(row.get("schedule_display") or ""),
            ]
        ).lower()
        if query not in hay:
            return False
    return True


def filter_cron_snapshot(
    snap: dict[str, Any],
    *,
    job_id: str = "",
    query: str = "",
) -> dict[str, Any]:
    """Return a copy of *snap* with jobs and running entries filtered."""
    job_id = (job_id or "").strip()
    query = (query or "").strip().lower()
    if not job_id and not query:
        return snap

    out = dict(snap)
    jobs_in = out.get("jobs") if isinstance(out.get("jobs"), list) else []
    jobs = [
        j for j in jobs_in if isinstance(j, dict) and _cron_row_matches_filter(
            j, job_id=job_id, query=query
        )
    ]
    active = [s for s in jobs if s.get("active")]
    paused = [s for s in jobs if not s.get("active")]
    errors = [s for s in active if s.get("last_status") == "error"]
    running_in = out.get("running") if isinstance(out.get("running"), list) else []
    running = [
        r
        for r in running_in
        if isinstance(r, dict)
        and _cron_row_matches_filter(r, job_id=job_id, query=query)
    ]
    out["jobs"] = jobs
    out["total"] = len(jobs)
    out["active_count"] = len(active)
    out["paused_count"] = len(paused)
    out["error_count"] = len(errors)
    out["running"] = running
    out["running_count"] = len(running)
    out["filtered"] = True
    if job_id:
        out["filter_job_id"] = job_id
    if query:
        out["filter_query"] = query
    return out


def _cron_health_summary(snap: dict[str, Any]) -> str:
    total = int(snap.get("total") or 0)
    active = int(snap.get("active_count") or 0)
    paused = int(snap.get("paused_count") or 0)
    errors = int(snap.get("error_count") or 0)
    running = int(snap.get("running_count") or 0)
    parts = [
        f"{total} job(s) com agenda",
        f"{active} ativo(s)",
        f"{paused} pausado(s)",
    ]
    if errors:
        parts.append(f"{errors} com última execução em erro")
    if running:
        parts.append(f"{running} em execução agora")
    return "; ".join(parts)


def _compact_cron_job_row(row: dict[str, Any], *, preview_chars: int) -> dict[str, Any]:
    preview = str(row.get("last_result_preview") or "").strip()
    if len(preview) > preview_chars:
        preview = preview[: preview_chars - 1] + "…"
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "enabled": row.get("enabled"),
        "active": row.get("active"),
        "state": row.get("state"),
        "schedule_display": row.get("schedule_display"),
        "next_run_at": row.get("next_run_at"),
        "last_run_at": row.get("last_run_at"),
        "last_status": row.get("last_status"),
        "profile": row.get("profile"),
        "last_result_preview": preview or None,
    }


def compact_cron_snapshot_for_chat(
    snap: dict[str, Any],
    *,
    max_jobs: int = 40,
    preview_chars: int = 200,
    ensure_job_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Trim Hermes cron snapshot for dashboard chat tool / LLM context."""
    if not snap.get("ok", True):
        return {
            "ok": False,
            "error": snap.get("error") or "falha ao ler cron Hermes",
        }
    jobs_in = snap.get("jobs") if isinstance(snap.get("jobs"), list) else []
    ids_needed = {
        str(jid).strip()
        for jid in (ensure_job_ids or [])
        if jid is not None and str(jid).strip()
    }
    jobs_out: list[dict[str, Any]] = []
    included: set[str] = set()
    lim = max(1, max_jobs)

    if ids_needed:
        for row in jobs_in:
            if not isinstance(row, dict):
                continue
            rid = str(row.get("id") or "").strip()
            if rid and rid in ids_needed:
                jobs_out.append(_compact_cron_job_row(row, preview_chars=preview_chars))
                included.add(rid)

    for row in jobs_in:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or "").strip()
        if rid and rid in included:
            continue
        if len(jobs_out) >= lim:
            break
        jobs_out.append(_compact_cron_job_row(row, preview_chars=preview_chars))
        if rid:
            included.add(rid)
    running_in = snap.get("running") if isinstance(snap.get("running"), list) else []
    running_out: list[dict[str, Any]] = []
    for row in running_in[:12]:
        if not isinstance(row, dict):
            continue
        msg = str(row.get("last_message") or "").strip()
        if len(msg) > 160:
            msg = msg[:159] + "…"
        running_out.append(
            {
                "job_id": row.get("job_id"),
                "name": row.get("name"),
                "profile": row.get("profile"),
                "started_at": row.get("started_at"),
                "seconds_since_activity": row.get("seconds_since_activity"),
                "last_message": msg or None,
            }
        )
    compact = {
        "ok": True,
        "jobs_path": snap.get("jobs_path"),
        "updated_at": snap.get("updated_at"),
        "total": snap.get("total"),
        "active_count": snap.get("active_count"),
        "paused_count": snap.get("paused_count"),
        "error_count": snap.get("error_count"),
        "running_count": snap.get("running_count"),
        "health_summary": _cron_health_summary(snap),
        "jobs": jobs_out,
        "running": running_out,
    }
    if len(jobs_in) > len(jobs_out):
        compact["jobs_truncated"] = True
    return compact


def find_job_id_by_name(job_name: str, jobs_path: Path) -> Optional[str]:
    jobs, _ = read_jobs_file(jobs_path)
    for job in jobs:
        if job.get("name") == job_name:
            jid = job.get("id")
            return str(jid) if jid is not None else None
    return None


def resolve_cron_job_ref(ref: str, jobs_path: Path) -> dict[str, Any]:
    """Resolve cron job by full id, name, or unique id prefix/suffix."""
    needle = (ref or "").strip()
    if not needle:
        return {"ok": False, "error": "job id is required"}

    jobs, _updated_at = read_jobs_file(jobs_path)

    needle_l = needle.lower()

    def _jid(job: dict[str, Any]) -> str:
        return str(job.get("id") or "")

    for job in jobs:
        if _jid(job) == needle:
            return {"ok": True, "job": job, "job_id": _jid(job)}

    for job in jobs:
        name = str(job.get("name") or "")
        if name == needle or name.lower() == needle_l:
            return {"ok": True, "job": job, "job_id": _jid(job)}

    prefix_matches = [
        job
        for job in jobs
        if _jid(job) and _jid(job).lower().startswith(needle_l)
    ]
    if len(prefix_matches) == 1:
        job = prefix_matches[0]
        return {
            "ok": True,
            "job": job,
            "job_id": _jid(job),
            "resolved_from": needle,
        }
    if len(prefix_matches) > 1:
        return {
            "ok": False,
            "error": (
                f"referência de job {needle!r} é ambígua "
                f"({len(prefix_matches)} jobs); use o id completo"
            ),
            "matches": [
                {"id": _jid(j), "name": j.get("name")} for j in prefix_matches[:10]
            ],
        }

    suffix_matches = [
        job
        for job in jobs
        if _jid(job) and _jid(job).lower().endswith(needle_l)
    ]
    if len(suffix_matches) == 1:
        job = suffix_matches[0]
        return {
            "ok": True,
            "job": job,
            "job_id": _jid(job),
            "resolved_from": needle,
        }

    return {
        "ok": False,
        "error": (
            f"job {needle!r} não encontrado; "
            "use o id completo ou cronjob(action='list')"
        ),
    }


def find_job_by_id(job_id: str, jobs_path: Path) -> Optional[dict[str, Any]]:
    """Return the raw job record from jobs.json, or None."""
    resolved = resolve_cron_job_ref(job_id, jobs_path)
    if resolved.get("ok"):
        job = resolved.get("job")
        return job if isinstance(job, dict) else None
    return None


def _parse_cron_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000.0, tz=datetime.now().astimezone().tzinfo)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _job_next_run_dt(job: dict[str, Any]) -> Optional[datetime]:
    """Hermes ``next_run_at`` or OpenClaw ``state.nextRunAtMs``."""
    dt = _parse_cron_timestamp(job.get("next_run_at"))
    if dt is not None:
        return dt
    state = job.get("state")
    if isinstance(state, dict):
        return _parse_cron_timestamp(state.get("nextRunAtMs"))
    return None


def _cron_job_is_paused(job: dict[str, Any]) -> bool:
    if not job.get("enabled", True):
        return True
    state = job.get("state")
    if isinstance(state, str):
        return state.lower() == "paused"
    return False


def _cron_job_is_running_locked(job: dict[str, Any]) -> bool:
    state = job.get("state")
    return isinstance(state, dict) and bool(state.get("runningAtMs"))


def _write_job_next_run(job: dict[str, Any], next_at: datetime) -> None:
    job["next_run_at"] = next_at.isoformat()
    ms = int(next_at.timestamp() * 1000)
    state = job.get("state")
    if isinstance(state, dict):
        state["nextRunAtMs"] = ms
    else:
        job["state"] = "scheduled"


def cron_next_run_is_plausible(
    job: dict[str, Any],
    *,
    grace_seconds: float = 300.0,
    now: Optional[datetime] = None,
) -> bool:
    """True when an idle cron still has an upcoming (or barely late) next run.

    Used by the kanban auto-start watchdog: a cron whose ``next_run_at`` is
    missing or overdue beyond the grace period is considered stuck, so the
    priority queue may take over the card instead of waiting forever.
    """
    next_dt = _job_next_run_dt(job)
    if next_dt is None:
        return False
    now = now or datetime.now().astimezone()
    if next_dt.tzinfo is None:
        next_dt = next_dt.replace(tzinfo=now.tzinfo)
    return next_dt > now - timedelta(seconds=grace_seconds)


def is_cron_job_overdue(job: dict[str, Any], *, now: Optional[datetime] = None) -> bool:
    """True when an enabled, non-paused scheduled job should have run already."""
    if not isinstance(job, dict):
        return False
    if _cron_job_is_paused(job) or _cron_job_is_running_locked(job):
        return False
    schedule = job.get("schedule")
    if not isinstance(schedule, dict) or not schedule:
        return False

    now = now or datetime.now().astimezone()
    next_dt = _job_next_run_dt(job)
    if next_dt is not None:
        return next_dt <= now

    kind = schedule.get("kind")
    if kind in {"cron", "interval"}:
        return True
    if kind == "once":
        run_at = _parse_cron_timestamp(schedule.get("run_at"))
        if run_at is None:
            return True
        return run_at <= now
    return False


def list_overdue_cron_jobs(
    jobs_path: Path,
    *,
    include_paused: bool = False,
) -> list[dict[str, Any]]:
    """Return enabled scheduled jobs whose next run time is missing or in the past."""
    jobs, _ = read_jobs_file(jobs_path)
    out: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if include_paused and str(job.get("state") or "").lower() == "paused":
            check = dict(job)
            check["state"] = "scheduled"
            if is_cron_job_overdue(check):
                out.append(job)
            continue
        if is_cron_job_overdue(job):
            out.append(job)
    now = datetime.now().astimezone()
    fallback_ts = datetime.fromtimestamp(0, tz=now.tzinfo)
    out.sort(
        key=lambda j, _fb=fallback_ts: (
            _job_next_run_dt(j)
            or _parse_cron_timestamp(
                (j.get("schedule") or {}).get("run_at")
                if isinstance(j.get("schedule"), dict)
                else None
            )
            or _fb,
            str(j.get("name") or ""),
        ),
    )
    return out


def catch_up_overdue_cron_jobs(
    jobs_path: Path,
    *,
    delay_base_seconds: int = MANUAL_CRON_RUN_DELAY_SECONDS,
    delay_step_seconds: int = 60,
    skip_running_ids: Optional[set[str]] = None,
    include_paused: bool = False,
    dry_run: bool = False,
    accept_hooks: bool = False,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Execute overdue jobs immediately (same semantics as ``hermes cron run``)."""
    overdue = list_overdue_cron_jobs(jobs_path, include_paused=include_paused)
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "mode": "run-now",
            "count": len(overdue),
            "jobs": [
                {
                    "id": j.get("id"),
                    "name": j.get("name"),
                    "next_run_at": j.get("next_run_at"),
                    "schedule_kind": (j.get("schedule") or {}).get("kind")
                    if isinstance(j.get("schedule"), dict)
                    else None,
                }
                for j in overdue
            ],
        }

    triggered: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    run_timeout = max(5.0, float(timeout_seconds))
    for index, job in enumerate(overdue):
        job_id = str(job.get("id") or "")
        if not job_id:
            continue
        if skip_running_ids and job_id in skip_running_ids:
            skipped.append({"job_id": job_id, "name": job.get("name"), "reason": "running"})
            continue

        cmd = hermes_cron_argv("run", job_id, accept_hooks=accept_hooks)
        result = run_hermes_cron_command(
            cmd,
            timeout_seconds=run_timeout,
            job_id=job_id,
            job_name=str(job.get("name") or job_id),
            jobs_path=jobs_path,
        )
        row = {
            "job_id": job_id,
            "name": job.get("name"),
            "index": index,
            "ok": result.get("ok"),
            "summary": result.get("summary"),
            "stdout_tail": result.get("stdout_tail"),
            "stderr_tail": result.get("stderr_tail"),
            "error": result.get("error"),
            "reason": result.get("reason"),
        }
        if result.get("ok"):
            triggered.append(row)
        else:
            skipped.append({**row, "reason": "run_failed"})

    return {
        "ok": True,
        "mode": "run-now",
        "count": len(overdue),
        "scheduled": triggered,
        "triggered": triggered,
        "skipped": skipped,
        "summary": (
            f"Triggered {len(triggered)} overdue cron job(s) immediately"
            + (f"; skipped {len(skipped)}" if skipped else "")
        ),
    }


def schedule_cron_job_manual_run(
    jobs_path: Path,
    job_id: str,
    *,
    delay_seconds: int = MANUAL_CRON_RUN_DELAY_SECONDS,
) -> dict[str, Any]:
    """Schedule a dashboard/manual cron run ``delay_seconds`` in the future."""
    jobs_path = jobs_path.expanduser().resolve()
    if not jobs_path.is_file():
        return {"ok": False, "error": f"jobs file not found: {jobs_path}"}

    try:
        raw_text = jobs_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}

    if isinstance(data, list):
        wrapper: dict[str, Any] = {"jobs": data}
    elif isinstance(data, dict):
        wrapper = data
    else:
        return {"ok": False, "error": "unexpected jobs.json shape"}

    jobs = wrapper.get("jobs")
    if not isinstance(jobs, list):
        return {"ok": False, "error": "jobs.json has no jobs list"}

    canon_id: Optional[str] = None
    job_name: Optional[str] = None
    next_at: Optional[datetime] = None
    for job in jobs:
        if not isinstance(job, dict) or str(job.get("id") or "") != str(job_id):
            continue
        canon_id = str(job_id)
        job_name = str(job.get("name") or job_id)
        next_at = datetime.now().astimezone() + timedelta(seconds=max(delay_seconds, 1))
        job["enabled"] = True
        if not isinstance(job.get("state"), dict):
            job["state"] = "scheduled"
        job["paused_at"] = None
        job["paused_reason"] = None
        _write_job_next_run(job, next_at)
        break

    if not canon_id or next_at is None:
        return {"ok": False, "error": f"job not found: {job_id}"}

    wrapper["updated_at"] = datetime.now().astimezone().isoformat()
    jobs_path.write_text(
        json.dumps(wrapper, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "ok": True,
        "job_id": canon_id,
        "job_name": job_name,
        "next_run_at": next_at.isoformat(),
        "delay_seconds": delay_seconds,
        "summary": (
            f"Triggered job: {job_name} ({canon_id}) — "
            f"next run in {delay_seconds}s"
        ),
    }


def is_recurring_cron_schedule(schedule: Any) -> bool:
    """True for cron/interval schedules (not one-shot ``once``)."""
    if not isinstance(schedule, dict):
        return False
    return schedule.get("kind") in {"cron", "interval"}


def compute_next_run_at_for_job(
    job: dict[str, Any],
    *,
    reference: Optional[datetime] = None,
    anchor_from_last_run: bool = True,
) -> Optional[str]:
    """Compute the next ISO run time for a recurring Hermes cron job."""
    schedule = job.get("schedule")
    if not isinstance(schedule, dict):
        return None
    kind = schedule.get("kind")
    if kind == "once":
        run_at = schedule.get("run_at")
        return str(run_at) if run_at else None

    reference = reference or datetime.now().astimezone()
    base = reference
    if anchor_from_last_run:
        last_run_raw = job.get("last_run_at")
        if last_run_raw:
            try:
                parsed = datetime.fromisoformat(str(last_run_raw).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=reference.tzinfo)
                base = parsed
            except ValueError:
                base = reference

    if kind == "interval":
        minutes = int(schedule.get("minutes") or 0)
        if minutes <= 0:
            return None
        return (base + timedelta(minutes=minutes)).isoformat()

    if kind == "cron":
        expr = str(schedule.get("expr") or "").strip()
        if not expr or croniter is None:
            return None
        try:
            return croniter(expr, base).get_next(datetime).isoformat()
        except Exception:
            return None
    return None


def compute_upcoming_runs_for_job(
    job: dict[str, Any],
    *,
    limit: int = 8,
    horizon_hours: float = 48.0,
    reference: Optional[datetime] = None,
) -> list[str]:
    """Return ISO timestamps for upcoming runs within *horizon_hours*."""
    schedule = job.get("schedule")
    if not isinstance(schedule, dict):
        next_iso = job.get("next_run_at")
        return [str(next_iso)] if next_iso else []

    reference = reference or datetime.now().astimezone()
    horizon_end = reference + timedelta(hours=max(horizon_hours, 0.25))
    kind = schedule.get("kind")
    runs: list[str] = []

    if kind == "once":
        run_at = schedule.get("run_at")
        if not run_at:
            return runs
        try:
            parsed = datetime.fromisoformat(str(run_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=reference.tzinfo)
        except ValueError:
            return runs
        if reference <= parsed <= horizon_end:
            runs.append(parsed.isoformat())
        return runs

    base = reference
    last_run_raw = job.get("last_run_at")
    if last_run_raw:
        try:
            parsed = datetime.fromisoformat(str(last_run_raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=reference.tzinfo)
            base = parsed
        except ValueError:
            base = reference

    max_runs = max(1, min(int(limit or 1), 32))
    cursor = base
    for _ in range(max_runs):
        probe = dict(job)
        probe["schedule"] = schedule
        next_iso = compute_next_run_at_for_job(
            probe,
            reference=cursor,
            anchor_from_last_run=False,
        )
        if not next_iso:
            break
        try:
            next_dt = datetime.fromisoformat(str(next_iso).replace("Z", "+00:00"))
            if next_dt.tzinfo is None:
                next_dt = next_dt.replace(tzinfo=reference.tzinfo)
        except ValueError:
            break
        if next_dt > horizon_end:
            break
        if next_dt >= reference:
            runs.append(next_dt.isoformat())
        cursor = next_dt + timedelta(seconds=1)
        if len(runs) >= max_runs:
            break
    return runs


def finalize_recurring_cron_job(
    job: dict[str, Any],
    *,
    reference: Optional[datetime] = None,
) -> bool:
    """Keep recurring jobs enabled and scheduled with a future ``next_run_at``."""
    if not is_recurring_cron_schedule(job.get("schedule")):
        return False

    # Never auto-resume jobs that were explicitly paused by the user.
    if job.get("paused_at") and _cron_job_is_paused(job):
        return False

    reference = reference or datetime.now().astimezone()
    changed = False

    if not job.get("enabled", True):
        job["enabled"] = True
        changed = True

    state = job.get("state")
    if isinstance(state, dict):
        if state.get("runningAtMs"):
            return changed
    elif not isinstance(state, str) or state.lower() in {
        "paused",
        "completed",
        "error",
    }:
        job["state"] = "scheduled"
        job["paused_at"] = None
        job["paused_reason"] = None
        changed = True

    next_iso = compute_next_run_at_for_job(job, reference=reference)
    if not next_iso:
        return changed

    try:
        next_dt = datetime.fromisoformat(str(next_iso).replace("Z", "+00:00"))
    except ValueError:
        return changed
    if next_dt.tzinfo is None:
        next_dt = next_dt.replace(tzinfo=reference.tzinfo)

    attempts = 0
    while next_dt <= reference and attempts < 64:
        probe = compute_next_run_at_for_job(
            job,
            reference=next_dt + timedelta(seconds=1),
            anchor_from_last_run=False,
        )
        if not probe:
            break
        try:
            next_dt = datetime.fromisoformat(str(probe).replace("Z", "+00:00"))
            if next_dt.tzinfo is None:
                next_dt = next_dt.replace(tzinfo=reference.tzinfo)
        except ValueError:
            break
        attempts += 1

    current = _job_next_run_dt(job)
    if current is None or abs((current - next_dt).total_seconds()) > 1:
        _write_job_next_run(job, next_dt)
        changed = True
    return changed


def list_stale_recurring_cron_jobs(
    jobs_path: Path,
    *,
    stale_hours: float = 48.0,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Recurring jobs that look stuck (overdue or not scheduled for > stale_hours)."""
    jobs, _ = read_jobs_file(jobs_path)
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(hours=max(1.0, float(stale_hours)))
    stale: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict) or not is_recurring_cron_schedule(job.get("schedule")):
            continue
        if job.get("paused_at") and _cron_job_is_paused(job):
            continue
        last_run = _parse_cron_timestamp(job.get("last_run_at"))
        overdue = is_cron_job_overdue(job, now=now)
        plausible = cron_next_run_is_plausible(job, grace_seconds=0, now=now)
        needs_attention = (
            not job.get("enabled", True)
            or _cron_job_is_paused(job)
            or overdue
            or not plausible
        )
        if not needs_attention:
            continue
        if last_run is not None and last_run > cutoff and not overdue:
            continue
        stale.append(
            {
                "id": str(job.get("id") or ""),
                "name": str(job.get("name") or job.get("id") or "?"),
                "state": str(job.get("state") or ""),
                "enabled": bool(job.get("enabled", True)),
                "next_run_at": job.get("next_run_at"),
                "last_run_at": job.get("last_run_at"),
                "last_status": job.get("last_status"),
                "overdue": overdue,
            }
        )
    return stale


def maintain_recurring_cron_schedule(
    jobs_path: Path,
    *,
    stale_hours: float = 48.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Repair recurring jobs and report any that remain stale."""
    before_stale = list_stale_recurring_cron_jobs(
        jobs_path,
        stale_hours=stale_hours,
    )
    repair = repair_recurring_cron_jobs(jobs_path, dry_run=dry_run)
    after_stale = (
        []
        if dry_run
        else list_stale_recurring_cron_jobs(jobs_path, stale_hours=stale_hours)
    )
    repaired = int(repair.get("repaired") or 0)
    summary_parts = []
    if repaired:
        summary_parts.append(f"reagendados {repaired}")
    if after_stale:
        summary_parts.append(f"{len(after_stale)} ainda preso(s)")
    return {
        **repair,
        "stale_before": len(before_stale),
        "stale_after": len(after_stale),
        "stale_jobs": after_stale[:12],
        "summary": (
            "Manutenção cron: " + ", ".join(summary_parts)
            if summary_parts
            else "Manutenção cron: nada a corrigir"
        ),
    }


def repair_recurring_cron_jobs(
    jobs_path: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Re-enable recurring jobs and recompute ``next_run_at`` after runs."""
    jobs_path = jobs_path.expanduser().resolve()
    if not jobs_path.is_file():
        return {"ok": False, "error": f"jobs file not found: {jobs_path}", "repaired": 0}

    try:
        raw_text = jobs_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc), "repaired": 0}

    if isinstance(data, list):
        wrapper: dict[str, Any] = {"jobs": data}
    elif isinstance(data, dict):
        wrapper = data
    else:
        return {"ok": False, "error": "unexpected jobs.json shape", "repaired": 0}

    jobs = wrapper.get("jobs")
    if not isinstance(jobs, list):
        return {"ok": False, "error": "jobs.json has no jobs list", "repaired": 0}

    repaired: list[dict[str, str]] = []
    for job in jobs:
        if not isinstance(job, dict) or not is_recurring_cron_schedule(job.get("schedule")):
            continue
        # Skip jobs that were explicitly paused by the user (have paused_at set).
        # These should only be resumed via an explicit resume command.
        if job.get("paused_at") and _cron_job_is_paused(job):
            continue
        needs_repair = (
            not job.get("enabled", True)
            or _cron_job_is_paused(job)
            or is_cron_job_overdue(job)
        )
        if not needs_repair:
            continue
        before_next = job.get("next_run_at")
        before_enabled = job.get("enabled")
        before_state = job.get("state")
        if dry_run:
            repaired.append(
                {
                    "id": str(job.get("id") or ""),
                    "name": str(job.get("name") or ""),
                }
            )
            continue
        job_id = str(job.get("id") or "")
        if finalize_recurring_cron_job(job):
            repaired.append(
                {
                    "id": str(job.get("id") or ""),
                    "name": str(job.get("name") or ""),
                    "before_next_run_at": str(before_next or ""),
                    "after_next_run_at": str(job.get("next_run_at") or ""),
                    "before_enabled": str(before_enabled),
                    "after_enabled": str(job.get("enabled")),
                    "before_state": str(before_state or ""),
                    "after_state": str(job.get("state") or ""),
                }
            )

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "repaired": len(repaired),
            "jobs": repaired,
            "jobs_path": str(jobs_path),
        }

    if not repaired:
        return {"ok": True, "repaired": 0, "jobs_path": str(jobs_path)}

    wrapper["updated_at"] = datetime.now().astimezone().isoformat()
    jobs_path.write_text(
        json.dumps(wrapper, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "ok": True,
        "repaired": len(repaired),
        "jobs": repaired[:20],
        "jobs_path": str(jobs_path),
        "summary": f"Reagendados {len(repaired)} job(s) recorrente(s)",
    }


def job_detail_for_edit(job: dict[str, Any]) -> dict[str, Any]:
    """Fields the dashboard edit form needs."""
    schedule = job.get("schedule")
    builder = cron_schedule_to_builder(schedule)
    state = job.get("state")
    paused = (
        not job.get("enabled", True)
        or (isinstance(state, str) and state.lower() == "paused")
    )
    return {
        "id": job.get("id"),
        "name": job.get("name") or job.get("id") or "?",
        "schedule_display": job.get("schedule_display")
        or (schedule.get("display") if isinstance(schedule, dict) else None)
        or (schedule.get("expr") if isinstance(schedule, dict) else None)
        or builder.get("schedule_string"),
        "schedule": schedule,
        "schedule_string": builder.get("schedule_string"),
        "schedule_presets": cron_schedule_preset_catalog(),
        "paused": paused,
        "active": bool(job.get("enabled", True))
        and (not isinstance(state, str) or state.lower() != "paused"),
        "prompt": job.get("prompt") or "",
        "profile": job.get("profile"),
        "enabled": bool(job.get("enabled")),
        "state": state or "",
        "workdir": job.get("workdir"),
        "deliver": job.get("deliver"),
        **builder,
    }


def build_cron_create_argv(
    schedule: str,
    prompt: str,
    *,
    name: Optional[str] = None,
    profile: Optional[str] = None,
    model: Optional[str] = None,
    skills: Optional[list[str]] = None,
    workdir: Optional[str] = None,
    repeat: Optional[int] = None,
    accept_hooks: bool = False,
) -> list[str]:
    """Build argv for `hermes cron create`."""
    cmd = hermes_cron_argv("create", schedule, prompt, accept_hooks=accept_hooks, model=model)
    if name:
        cmd.extend(["--name", name])
    if profile:
        cmd.extend(["--profile", profile])
    if workdir:
        cmd.extend(["--workdir", workdir])
    if repeat is not None and repeat > 0:
        cmd.extend(["--repeat", str(int(repeat))])
    for skill in skills or []:
        cmd.extend(["--skill", skill])
    return cmd


def _parse_created_job_id(stdout: str) -> Optional[str]:
    for line in stdout.splitlines():
        line = line.strip()
        if line.lower().startswith("created job:"):
            return line.split(":", 1)[1].strip()
    return None


def resolve_created_cron_job_id(
    create_result: dict[str, Any],
    *,
    job_name: Optional[str] = None,
    jobs_path: Optional[Path] = None,
) -> Optional[str]:
    """Best-effort job id after `hermes cron create` (stdout or jobs.json lookup)."""
    job_id = create_result.get("job_id")
    if job_id:
        return str(job_id).strip() or None
    tail = str(create_result.get("stdout_tail") or create_result.get("stdout") or "")
    parsed = _parse_created_job_id(tail)
    if parsed:
        return parsed
    if job_name and jobs_path is not None:
        return find_job_id_by_name(job_name, jobs_path)
    return None


def create_hermes_cron_job(
    schedule: str,
    prompt: str,
    *,
    name: Optional[str] = None,
    profile: Optional[str] = None,
    model: Optional[str] = None,
    skills: Optional[list[str]] = None,
    workdir: Optional[str] = None,
    repeat: Optional[int] = None,
    accept_hooks: bool = False,
    timeout_seconds: float = 120.0,
    jobs_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Create a scheduled Hermes cron job via CLI."""
    cmd = build_cron_create_argv(
        schedule,
        prompt,
        name=name,
        profile=profile,
        model=model,
        skills=skills,
        workdir=workdir,
        repeat=repeat,
        accept_hooks=accept_hooks,
    )
    result = run_hermes_cron_command(
        cmd,
        timeout_seconds=timeout_seconds,
        job_name=name,
        jobs_path=jobs_path,
    )
    job_id = _parse_created_job_id(result.get("stdout_tail") or "")
    if result.get("ok"):
        result["job_id"] = job_id
        result["schedule"] = schedule
        result["profile"] = profile
        result["skills"] = list(skills or [])
        result["workdir"] = workdir
    return result


def update_hermes_cron_job(
    job_id: str,
    *,
    schedule: Optional[str] = None,
    name: Optional[str] = None,
    prompt: Optional[str] = None,
    profile: Optional[str] = None,
    accept_hooks: bool = False,
    timeout_seconds: float = 120.0,
    jobs_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Update an existing Hermes cron job via CLI."""
    cmd = build_cron_edit_argv(
        job_id,
        schedule=schedule,
        name=name,
        prompt=prompt,
        profile=profile,
        accept_hooks=accept_hooks,
    )
    result = run_hermes_cron_command(
        cmd,
        timeout_seconds=timeout_seconds,
        job_id=job_id,
        job_name=name,
        jobs_path=jobs_path,
    )
    if result.get("ok"):
        result["job_id"] = str(job_id)
        if schedule is not None:
            result["schedule"] = schedule
        if profile is not None:
            result["profile"] = profile
        result["reused"] = True
    return result


def build_cron_edit_argv(
    job_id: str,
    *,
    schedule: Optional[str] = None,
    name: Optional[str] = None,
    prompt: Optional[str] = None,
    profile: Optional[str] = None,
    accept_hooks: bool = False,
) -> list[str]:
    """Build argv for `hermes cron edit`. Only non-None fields are sent."""
    cmd = hermes_cron_argv("edit", str(job_id), accept_hooks=accept_hooks)
    if schedule is not None:
        cmd.extend(["--schedule", schedule])
    if name is not None:
        cmd.extend(["--name", name])
    if prompt is not None:
        cmd.extend(["--prompt", prompt])
    if profile is not None:
        cmd.extend(["--profile", profile])
    return cmd


def probe_hermes_cron_scheduler(jobs_path: Path) -> dict[str, Any]:
    """Check whether the gateway for ``jobs_path``'s HERMES_HOME will fire cron jobs."""
    jobs_path = jobs_path.expanduser().resolve()
    repair = repair_malformed_cron_schedules(jobs_path)
    home = hermes_home_from_jobs_path(jobs_path)
    cmd = hermes_cmd(*_HERMES_CRON_CLI_PROFILE, "cron", "status")
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    pretty = " ".join(shlex.quote(c) for c in cmd)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=45.0,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "gateway_running": False,
            "hermes_home": str(home),
            "command": pretty,
            "reason": f"{pretty} timed out",
        }
    stdout_text = proc.stdout or ""
    stderr_text = proc.stderr or ""
    text = f"{stdout_text}\n{stderr_text}"
    gateway_running = "Gateway is running" in text or "✓ Gateway" in text
    blocked = "will NOT fire" in text or "✗ Gateway" in text
    ok = gateway_running and not blocked
    active_jobs: Optional[int] = None
    m = re.search(r"(\d+)\s+active job", text)
    if m:
        active_jobs = int(m.group(1))
    summary = _summarize_hermes_cron_output(stdout_text, stderr_text, proc.returncode)
    return {
        "ok": ok,
        "gateway_running": gateway_running,
        "blocked": blocked,
        "hermes_home": str(home),
        "jobs_path": str(jobs_path),
        "active_jobs": active_jobs,
        "schedule_repair": repair if repair.get("repaired") else None,
        "command": pretty,
        "summary": summary,
        "stdout_tail": stdout_text[-1500:],
        "stderr_tail": stderr_text[-1500:],
        "rc": proc.returncode,
    }


_DIRECT_CRON_JOB_SCRIPT = """\
import os
import sys

job_id = os.environ.get("QCLAW_CRON_JOB_ID", "").strip()
if not job_id:
    sys.exit(2)

from cron.jobs import mark_job_run, resolve_job_ref
from cron.scheduler import _deliver_result, run_job, save_job_output

job = resolve_job_ref(job_id)
if not job:
    print(f"job not found: {job_id}", file=sys.stderr)
    sys.exit(2)

try:
    success, output, final_response, error = run_job(job)
    save_job_output(job["id"], output)
    deliver_content = (
        final_response
        if success
        else f"Cron job '{job.get('name', job['id'])}' failed:\\n{error}"
    )
    delivery_error = None
    if deliver_content.strip():
        try:
            delivery_error = _deliver_result(job, deliver_content)
        except Exception as exc:
            delivery_error = str(exc)
    if success and not (final_response or "").strip():
        success = False
        error = (
            "Agent completed but produced empty response "
            "(model error, timeout, or misconfiguration)"
        )
    mark_job_run(job["id"], success, error, delivery_error=delivery_error)
    sys.exit(0 if success else 1)
except Exception as exc:
    mark_job_run(job["id"], False, str(exc))
    print(str(exc), file=sys.stderr)
    sys.exit(1)
"""


def spawn_hermes_cron_job_direct(
    job_id: str,
    jobs_path: Path,
    *,
    accept_hooks: bool = False,
) -> dict[str, Any]:
    """Run one cron job immediately in a detached subprocess (bypasses scheduler queue)."""
    canon_id = str(job_id or "").strip()
    if not canon_id:
        return {"ok": False, "error": "job_id is required"}

    jobs_path = jobs_path.expanduser().resolve()
    home = hermes_home_from_jobs_path(jobs_path)
    job = find_job_by_id(canon_id, jobs_path)
    job_name = str((job or {}).get("name") or canon_id)

    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["QCLAW_CRON_JOB_ID"] = canon_id
    env["PATH"] = _augmented_path()
    if accept_hooks:
        env["HERMES_ACCEPT_HOOKS"] = "1"

    python_bin = resolve_hermes_python()
    log_dir = home / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(  # noqa: SIM115 — handed to Popen, closed with process
            log_dir / f"direct_run_{canon_id}.log", "a", encoding="utf-8"
        )
    except OSError:
        log_file = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            [python_bin, "-c", _DIRECT_CRON_JOB_SCRIPT],
            env=env,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    except OSError as exc:
        return {
            "ok": False,
            "job_id": canon_id,
            "job_name": job_name,
            "error": str(exc),
            "mode": "direct",
        }
    finally:
        if log_file is not subprocess.DEVNULL:
            log_file.close()

    return {
        "ok": True,
        "job_id": canon_id,
        "job_name": job_name,
        "mode": "direct",
        "pid": proc.pid,
        "summary": f"Execução direta iniciada para {job_name} (pid {proc.pid})",
    }


def run_hermes_cron_command(
    cmd: list[str],
    *,
    timeout_seconds: float,
    job_id: Optional[str] = None,
    job_name: Optional[str] = None,
    jobs_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Run `hermes cron …` synchronously (for dashboard HTTP handlers)."""
    if cmd and cmd[0] == "hermes":
        cmd = [resolve_hermes_bin(), *cmd[1:]]
    pretty = " ".join(shlex.quote(c) for c in cmd)
    env = os.environ.copy()
    if jobs_path is not None:
        env["HERMES_HOME"] = str(hermes_home_from_jobs_path(jobs_path))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "timeout": True,
            "job_id": job_id,
            "job_name": job_name,
            "timeout_seconds": timeout_seconds,
            "command": pretty,
            "reason": f"{pretty} timed out after {timeout_seconds}s",
        }
    stdout_text = proc.stdout or ""
    stderr_text = proc.stderr or ""
    cli_failed = _hermes_cli_output_indicates_failure(stdout_text, stderr_text)
    ok = proc.returncode == 0 and not cli_failed
    summary = _summarize_hermes_cron_output(stdout_text, stderr_text, proc.returncode)
    return {
        "ok": ok,
        "rc": proc.returncode,
        "job_id": job_id,
        "job_name": job_name,
        "command": pretty,
        "stdout_tail": stdout_text[-2000:],
        "stderr_tail": stderr_text[-2000:],
        "summary": summary,
        **({"reason": summary} if not ok else {}),
    }


@dataclass(frozen=True)
class CronJobsPauseSnapshot:
    """Job ids that were active and were paused for agent creation."""

    paused_job_ids: tuple[str, ...] = ()


def pause_all_active_hermes_cron_jobs(
    jobs_path: Path,
    *,
    accept_hooks: bool = False,
    timeout_seconds: float = 60.0,
) -> tuple[CronJobsPauseSnapshot, dict[str, Any]]:
    """Pause every active cron job; return snapshot for selective resume."""
    jobs, _ = read_jobs_file(jobs_path)
    output_root = cron_output_dir_for_jobs_path(jobs_path)
    active_ids: list[str] = []
    for job in jobs:
        summary = summarize_cron_job(job, output_root=output_root)
        jid = summary.get("id")
        if summary.get("active") and jid is not None:
            active_ids.append(str(jid))

    failures: list[dict[str, Any]] = []
    pause_timeout = min(timeout_seconds, 60.0)
    for jid in active_ids:
        cmd = hermes_cron_argv("pause", jid, accept_hooks=accept_hooks)
        job = find_job_by_id(jid, jobs_path)
        result = run_hermes_cron_command(
            cmd,
            timeout_seconds=pause_timeout,
            job_id=jid,
            job_name=str((job or {}).get("name") or jid),
            jobs_path=jobs_path,
        )
        if not result.get("ok"):
            failures.append({"job_id": jid, **result})

    snapshot = CronJobsPauseSnapshot(paused_job_ids=tuple(active_ids))
    return snapshot, {
        "ok": not failures,
        "paused_count": len(active_ids),
        "failures": failures,
    }


def resume_hermes_cron_jobs_from_snapshot(
    snapshot: CronJobsPauseSnapshot,
    jobs_path: Path,
    *,
    accept_hooks: bool = False,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Resume only jobs that were paused by :func:`pause_all_active_hermes_cron_jobs`."""
    if not snapshot.paused_job_ids:
        return {"ok": True, "resumed_count": 0, "failures": []}

    failures: list[dict[str, Any]] = []
    resumed = 0
    resume_timeout = min(timeout_seconds, 60.0)
    for jid in snapshot.paused_job_ids:
        cmd = hermes_cron_argv("resume", jid, accept_hooks=accept_hooks)
        job = find_job_by_id(jid, jobs_path)
        result = run_hermes_cron_command(
            cmd,
            timeout_seconds=resume_timeout,
            job_id=jid,
            job_name=str((job or {}).get("name") or jid),
            jobs_path=jobs_path,
        )
        if result.get("ok"):
            resumed += 1
        else:
            failures.append({"job_id": jid, **result})

    return {
        "ok": not failures,
        "resumed_count": resumed,
        "failures": failures,
    }


def list_paused_cron_job_ids(jobs_path: Path) -> list[str]:
    """Return ids of cron jobs whose state is explicitly ``paused``."""
    jobs, _ = read_jobs_file(jobs_path)
    ids: list[str] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        state = job.get("state")
        if not isinstance(state, str) or state.lower() != "paused":
            continue
        jid = job.get("id")
        if jid is not None:
            ids.append(str(jid))
    return ids


def resume_all_paused_hermes_cron_jobs(
    jobs_path: Path,
    *,
    accept_hooks: bool = False,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Resume every cron job in ``paused`` state."""
    paused_ids = list_paused_cron_job_ids(jobs_path)
    if not paused_ids:
        return {"ok": True, "resumed_count": 0, "paused_count": 0, "failures": []}
    snapshot = CronJobsPauseSnapshot(paused_job_ids=tuple(paused_ids))
    meta = resume_hermes_cron_jobs_from_snapshot(
        snapshot,
        jobs_path,
        accept_hooks=accept_hooks,
        timeout_seconds=timeout_seconds,
    )
    meta["paused_count"] = len(paused_ids)
    return meta


# ---------------------------------------------------------------------------
# Cron schedule stagger
#
# Multiple Hermes jobs that share the same minute (e.g. `0 H * * *`, `0 */2 *
# * *`) all fire simultaneously and pile up tool-iteration budget on the
# OpenClaw side. The helpers below detect those collisions and produce
# replacement schedules that keep the same fire frequency but pick distinct
# minute slots, spaced by `step` minutes.
# ---------------------------------------------------------------------------

_CRON_5_FIELDS_RE = re.compile(r"^\s*(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$")
_PLAIN_INT_RE = re.compile(r"^\d+$")
_EVERY_INTERVAL_RE = re.compile(
    r"^\s*every\s+(\d+)\s*(h|hr|hour|hours|m|min|minute|minutes)\s*$",
    re.IGNORECASE,
)


def _parse_simple_cron_minute(expr: str) -> Optional[int]:
    """Return the integer minute when *expr* is a 5-field cron with a fixed minute."""
    m = _CRON_5_FIELDS_RE.match(expr or "")
    if not m:
        return None
    minute = m.group(1)
    if _PLAIN_INT_RE.match(minute):
        return int(minute)
    return None


def _set_simple_cron_minute(expr: str, minute: int) -> Optional[str]:
    """Replace the minute field of a 5-field cron expression."""
    m = _CRON_5_FIELDS_RE.match(expr or "")
    if not m:
        return None
    return f"{minute} {m.group(2)} {m.group(3)} {m.group(4)} {m.group(5)}"


def _interval_to_cron(schedule: str, *, base_hour: int) -> Optional[str]:
    """Convert "every Nh"/"every Nm" to a clock-aligned cron when feasible.

    Only emits a cron expression when the interval evenly divides the day/hour
    (24, 12, 8, 6, 4, 3, 2, 1 for hours; 30/20/15/12/10/6/5/4/3/2/1 for minutes),
    so that the rewrite preserves the original cadence exactly.
    """
    m = _EVERY_INTERVAL_RE.match(schedule or "")
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    if n <= 0:
        return None
    base_hour = max(0, min(23, int(base_hour)))
    if unit.startswith("h"):
        if n >= 24:
            return f"0 {base_hour} * * *"
        if 24 % n == 0:
            return f"0 */{n} * * *"
        return None
    # minutes
    if 60 % n == 0 and n < 60:
        return f"*/{n} * * * *"
    return None


def _pick_next_minute(occupied: set[int], step: int) -> int:
    """Return the smallest minute slot in [0,60) that isn't in *occupied*.

    Walks step-aligned slots first (0, step, 2*step, …) then any free minute.
    Falls back to ``(max(occupied) + step) % 60`` when fully saturated.
    """
    step = max(1, int(step))
    for base in range(0, 60, step):
        if base not in occupied:
            return base
    for minute in range(0, 60):
        if minute not in occupied:
            return minute
    if not occupied:
        return 0
    return (max(occupied) + step) % 60


def occupied_cron_minutes(jobs: list[dict[str, Any]]) -> set[int]:
    """Collect minute fields already taken by cron-kind jobs in *jobs*."""
    minutes: set[int] = set()
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        sched = job.get("schedule")
        if not isinstance(sched, dict) or sched.get("kind") != "cron":
            continue
        m = _parse_simple_cron_minute(str(sched.get("expr") or ""))
        if m is not None:
            minutes.add(m)
    return minutes


def stagger_cron_schedule(
    requested_schedule: str,
    *,
    existing_minutes: set[int],
    step: int = 15,
    base_hour: int = 9,
    convert_intervals: bool = True,
) -> str:
    """Return a schedule with a non-colliding minute when feasible.

    Leaves the schedule untouched when it isn't a simple cron expression (or
    convertible interval) — we don't want to silently change semantics on
    unusual patterns. When the minute is already unique, returns the (possibly
    converted) expression unchanged.
    """
    expr = (requested_schedule or "").strip()
    if not expr:
        return expr

    candidate = expr
    current_minute = _parse_simple_cron_minute(candidate)
    if current_minute is None and convert_intervals:
        converted = _interval_to_cron(candidate, base_hour=base_hour)
        if converted is not None:
            candidate = converted
            current_minute = _parse_simple_cron_minute(candidate)

    if current_minute is None:
        return requested_schedule

    if current_minute not in existing_minutes:
        return candidate

    new_minute = _pick_next_minute(existing_minutes, step)
    return _set_simple_cron_minute(candidate, new_minute) or candidate


def plan_cron_stagger_rewrites(
    jobs: list[dict[str, Any]],
    *,
    step: int = 15,
    base_hour: int = 9,
) -> list[dict[str, Any]]:
    """Plan minute reassignments so existing cron-kind jobs stop colliding.

    Returns a list of ``{"job_id", "name", "expr", "new_expr", "minute",
    "new_minute"}`` entries for jobs whose schedule must change. The first job
    in each colliding minute bucket keeps its slot; later ones receive new
    minutes picked via :func:`_pick_next_minute`. Non-cron jobs and cron
    expressions without a fixed minute (``*``, ``*/N`` in the minute slot) are
    ignored — we don't have a safe rewrite for those.
    """
    minute_to_jobs: dict[int, list[dict[str, Any]]] = {}
    cron_jobs: list[tuple[dict[str, Any], int, str]] = []
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        sched = job.get("schedule")
        if not isinstance(sched, dict) or sched.get("kind") != "cron":
            continue
        expr = str(sched.get("expr") or "")
        minute = _parse_simple_cron_minute(expr)
        if minute is None:
            continue
        cron_jobs.append((job, minute, expr))
        minute_to_jobs.setdefault(minute, []).append(job)

    rewrites: list[dict[str, Any]] = []
    # Start from the minutes already occupied by jobs that we won't move (the
    # first job in each collision bucket keeps its slot).
    occupied: set[int] = set(minute_to_jobs.keys())
    for job, minute, expr in cron_jobs:
        peers = minute_to_jobs.get(minute) or []
        if len(peers) <= 1:
            continue
        if job is peers[0]:
            continue  # keep the first job in the bucket where it is
        new_minute = _pick_next_minute(occupied, step)
        new_expr = _set_simple_cron_minute(expr, new_minute)
        if not new_expr:
            continue
        occupied.add(new_minute)
        rewrites.append(
            {
                "job_id": str(job.get("id") or ""),
                "name": str(job.get("name") or ""),
                "expr": expr,
                "new_expr": new_expr,
                "minute": minute,
                "new_minute": new_minute,
            }
        )
    return rewrites
