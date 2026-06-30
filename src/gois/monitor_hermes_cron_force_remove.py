"""Hermes cron force-remove and profile delete."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class MonitorHermesCronForceRemoveMixin:
    def handle_hermes_cron_force_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Force-remove a cron job id/name from every jobs.json under known Hermes homes."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}

        needle = str(
            (payload or {}).get("job_id")
            or (payload or {}).get("id")
            or (payload or {}).get("job_name")
            or ""
        ).strip()
        if not needle:
            return {"ok": False, "error": "job_id is required"}

        import json as _json
        from datetime import datetime, timezone

        needle_l = needle.lower()
        removed_total = 0
        locations: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        removed_job_ids: set[str] = set()

        active_jobs_path = self._hermes_cron_jobs_path()
        active_home = hermes_home_from_jobs_path(active_jobs_path)

        homes: list[Path] = []
        seen_homes: set[Path] = set()
        for home in (
            active_home,
            Path.home() / ".hermes",
            Path("/Volumes/NAUBER/HomeOffload/hermes"),
        ):
            h = home.expanduser().resolve()
            if h in seen_homes or not h.is_dir():
                continue
            seen_homes.add(h)
            homes.append(h)

        candidate_files: list[Path] = []
        seen_files: set[Path] = set()
        for home in homes:
            root_jobs = (home / "cron" / "jobs.json").resolve()
            if root_jobs.is_file() and root_jobs not in seen_files:
                seen_files.add(root_jobs)
                candidate_files.append(root_jobs)

            profiles_root = home / "profiles"
            if not profiles_root.is_dir():
                continue
            for profile_dir in profiles_root.iterdir():
                if not profile_dir.is_dir() or profile_dir.name.startswith("."):
                    continue
                jobs_path = (profile_dir / "cron" / "jobs.json").resolve()
                if jobs_path.is_file() and jobs_path not in seen_files:
                    seen_files.add(jobs_path)
                    candidate_files.append(jobs_path)

        for jobs_path in candidate_files:
            try:
                raw = jobs_path.read_text(encoding="utf-8")
                data = _json.loads(raw)
            except _json.JSONDecodeError:
                # Retry with strict=False (handles bare control chars)
                try:
                    data = _json.loads(raw, strict=False)
                except Exception:
                    # JSON is truly corrupted — attempt text-based removal
                    repaired = self._force_remove_from_corrupted_jobs_file(
                        jobs_path, raw, needle, needle_l
                    )
                    if repaired is not None:
                        removed_total += repaired["removed_count"]
                        for jid in repaired.get("removed_job_ids", []):
                            removed_job_ids.add(jid)
                        locations.append(
                            {
                                "jobs_path": str(jobs_path),
                                "removed_count": repaired["removed_count"],
                                "removed": repaired["removed"],
                                "repaired_corrupted": True,
                            }
                        )
                    else:
                        errors.append(
                            {
                                "jobs_path": str(jobs_path),
                                "error": "JSON corrupted — could not locate job in raw text",
                            }
                        )
                    continue
            except Exception as exc:
                errors.append({"jobs_path": str(jobs_path), "error": f"{type(exc).__name__}: {exc}"})
                continue

            wrapped = isinstance(data, dict)
            if isinstance(data, list):
                jobs = data
            elif isinstance(data, dict):
                jobs = data.get("jobs")
            else:
                jobs = None
            if not isinstance(jobs, list):
                continue

            kept: list[dict[str, Any]] = []
            removed_here: list[dict[str, str]] = []
            for row in jobs:
                if not isinstance(row, dict):
                    kept.append(row)
                    continue
                jid = str(row.get("id") or "").strip()
                name = str(row.get("name") or "").strip()
                match = (jid == needle) or (name and name.lower() == needle_l)
                if match:
                    removed_here.append({"id": jid, "name": name})
                    if jid:
                        removed_job_ids.add(jid)
                    continue
                kept.append(row)

            if not removed_here:
                continue

            try:
                if wrapped:
                    out = dict(data)
                    out["jobs"] = kept
                    out["updated_at"] = datetime.now(timezone.utc).isoformat()
                    payload_text = _json.dumps(out, ensure_ascii=False, indent=2)
                else:
                    payload_text = _json.dumps(kept, ensure_ascii=False, indent=2)
                jobs_path.write_text(payload_text + "\n", encoding="utf-8")
            except Exception as exc:
                errors.append({"jobs_path": str(jobs_path), "error": f"{type(exc).__name__}: {exc}"})
                continue

            removed_total += len(removed_here)
            locations.append(
                {
                    "jobs_path": str(jobs_path),
                    "removed_count": len(removed_here),
                    "removed": removed_here,
                }
            )

        if removed_total > 0:
            self._invalidate_hermes_cron_cache()

        return {
            "ok": True,
            "job_ref": needle,
            "removed_count": removed_total,
            "removed_job_ids": sorted(removed_job_ids),
            "locations": locations,
            "homes_checked": [str(h) for h in homes],
            "message": (
                f"{removed_total} ocorrência(s) removida(s) à força"
                if removed_total
                else "job não encontrado nos jobs.json varridos"
            ),
            "errors": errors,
        }

    def _force_remove_from_corrupted_jobs_file(
        self,
        jobs_path: Path,
        raw_text: str,
        needle: str,
        needle_lower: str,
    ) -> Optional[dict[str, Any]]:
        """Remove a job entry from a corrupted jobs.json via text matching.

        When JSON is unrecoverable, locate the job object block by its
        id/name in the raw text, remove the brace-balanced block, and
        rewrite the file.  Returns removal info or None if not found.
        """
        import json as _json
        import re
        from datetime import datetime, timezone

        id_pat = re.compile(
            r'"id"\s*:\s*"' + re.escape(needle) + r'"', re.IGNORECASE
        )
        name_pat = re.compile(
            r'"name"\s*:\s*"' + re.escape(needle) + r'"', re.IGNORECASE
        )

        matches = list(id_pat.finditer(raw_text))
        if not matches:
            matches = list(name_pat.finditer(raw_text))
        if not matches:
            return None

        removed_ranges: list[tuple[int, int]] = []
        removed_entries: list[dict[str, str]] = []
        removed_ids: list[str] = []

        for m in matches:
            obj_start = None
            for i in range(m.start(), -1, -1):
                if raw_text[i] == '{':
                    obj_start = i
                    break
            if obj_start is None:
                continue

            depth = 0
            obj_end = None
            for i in range(obj_start, len(raw_text)):
                if raw_text[i] == '{':
                    depth += 1
                elif raw_text[i] == '}':
                    depth -= 1
                    if depth == 0:
                        obj_end = i + 1
                        break
            if obj_end is None:
                continue

            block = raw_text[obj_start:obj_end]
            jid = ""
            jname = ""
            id_m = re.search(r'"id"\s*:\s*"([^"]*)"', block)
            if id_m:
                jid = id_m.group(1)
            name_m = re.search(r'"name"\s*:\s*"([^"]*)"', block)
            if name_m:
                jname = name_m.group(1)

            removed_ranges.append((obj_start, obj_end))
            removed_entries.append({"id": jid, "name": jname})
            if jid:
                removed_ids.append(jid)

        if not removed_ranges:
            return None

        cleaned = raw_text
        for start, end in sorted(removed_ranges, reverse=True):
            before = cleaned[:start].rstrip()
            after = cleaned[end:].lstrip()
            if after.startswith(','):
                after = after[1:].lstrip()
            elif before.endswith(','):
                before = before[:-1].rstrip()
            cleaned = before + "\n" + after

        try:
            data = _json.loads(cleaned, strict=False)
            if isinstance(data, dict) and "jobs" in data:
                data["updated_at"] = datetime.now(timezone.utc).isoformat()
                final_text = _json.dumps(data, ensure_ascii=False, indent=2)
            elif isinstance(data, list):
                final_text = _json.dumps(
                    {"jobs": data, "updated_at": datetime.now(timezone.utc).isoformat()},
                    ensure_ascii=False, indent=2,
                )
            else:
                final_text = cleaned
        except Exception:
            final_text = cleaned

        try:
            jobs_path.write_text(final_text + "\n", encoding="utf-8")
        except Exception:
            return None

        return {
            "removed_count": len(removed_entries),
            "removed": removed_entries,
            "removed_job_ids": removed_ids,
        }

    def handle_hermes_cron_force_remove_all(self) -> dict[str, Any]:
        """Force-remove ALL cron jobs from every jobs.json under known Hermes homes."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}

        import json as _json
        from datetime import datetime, timezone

        removed_total = 0
        files_cleaned = 0
        locations: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        removed_job_ids: list[str] = []

        active_jobs_path = self._hermes_cron_jobs_path()
        active_home = hermes_home_from_jobs_path(active_jobs_path)

        homes: list[Path] = []
        seen_homes: set[Path] = set()
        for home in (
            active_home,
            Path.home() / ".hermes",
            Path("/Volumes/NAUBER/HomeOffload/hermes"),
        ):
            h = home.expanduser().resolve()
            if h in seen_homes or not h.is_dir():
                continue
            seen_homes.add(h)
            homes.append(h)

        candidate_files: list[Path] = []
        seen_files: set[Path] = set()
        for home in homes:
            root_jobs = (home / "cron" / "jobs.json").resolve()
            if root_jobs.is_file() and root_jobs not in seen_files:
                seen_files.add(root_jobs)
                candidate_files.append(root_jobs)

            profiles_root = home / "profiles"
            if not profiles_root.is_dir():
                continue
            for profile_dir in profiles_root.iterdir():
                if not profile_dir.is_dir() or profile_dir.name.startswith("."):
                    continue
                jobs_path = (profile_dir / "cron" / "jobs.json").resolve()
                if jobs_path.is_file() and jobs_path not in seen_files:
                    seen_files.add(jobs_path)
                    candidate_files.append(jobs_path)

        for jobs_path in candidate_files:
            try:
                raw = jobs_path.read_text(encoding="utf-8")
                data = _json.loads(raw)
            except _json.JSONDecodeError:
                try:
                    data = _json.loads(raw, strict=False)
                except Exception:
                    # Corrupted — overwrite with empty jobs list
                    try:
                        empty = _json.dumps(
                            {"jobs": [], "updated_at": datetime.now(timezone.utc).isoformat()},
                            ensure_ascii=False, indent=2,
                        )
                        jobs_path.write_text(empty + "\n", encoding="utf-8")
                        files_cleaned += 1
                        locations.append({
                            "jobs_path": str(jobs_path),
                            "removed_count": "unknown (corrupted)",
                            "repaired_corrupted": True,
                        })
                    except Exception as exc:
                        errors.append({"jobs_path": str(jobs_path), "error": f"{type(exc).__name__}: {exc}"})
                    continue
            except Exception as exc:
                errors.append({"jobs_path": str(jobs_path), "error": f"{type(exc).__name__}: {exc}"})
                continue

            wrapped = isinstance(data, dict)
            if isinstance(data, list):
                jobs = data
            elif isinstance(data, dict):
                jobs = data.get("jobs")
            else:
                jobs = None
            if not isinstance(jobs, list) or not jobs:
                continue

            # Collect all job IDs before clearing
            for row in jobs:
                if isinstance(row, dict):
                    jid = str(row.get("id") or "").strip()
                    if jid:
                        removed_job_ids.append(jid)

            removed_here = len(jobs)
            removed_total += removed_here

            try:
                if wrapped:
                    out = dict(data)
                    out["jobs"] = []
                    out["updated_at"] = datetime.now(timezone.utc).isoformat()
                    payload_text = _json.dumps(out, ensure_ascii=False, indent=2)
                else:
                    payload_text = _json.dumps([], ensure_ascii=False, indent=2)
                jobs_path.write_text(payload_text + "\n", encoding="utf-8")
                files_cleaned += 1
            except Exception as exc:
                errors.append({"jobs_path": str(jobs_path), "error": f"{type(exc).__name__}: {exc}"})
                continue

            locations.append({
                "jobs_path": str(jobs_path),
                "removed_count": removed_here,
            })

        if removed_total > 0:
            self._invalidate_hermes_cron_cache()

        return {
            "ok": True,
            "removed_count": removed_total,
            "removed_job_ids": sorted(set(removed_job_ids)),
            "files_cleaned": files_cleaned,
            "locations": locations,
            "homes_checked": [str(h) for h in homes],
            "message": (
                f"{removed_total} job(s) removido(s) à força de {files_cleaned} arquivo(s)"
                if removed_total
                else "Nenhum job encontrado nos jobs.json varridos"
            ),
            "errors": errors,
        }

    def handle_hermes_profile_delete(self, profile_name: str) -> dict[str, Any]:
        """Delete a Hermes agent profile via the dashboard API."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        name = str(profile_name or "").strip()
        if not name:
            return {"ok": False, "error": "profile name is required"}
        if name.lower() == "default":
            return {"ok": False, "error": "cannot delete the default profile"}
        dashboard_url = self._hermes_dashboard_url()
        if not dashboard_url:
            return {"ok": False, "error": "hermes dashboard URL not configured"}
        import httpx
        from .hermes_profiles import _api_base, _hermes_api_call
        api_base = _api_base(dashboard_url)
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = _hermes_api_call(
                    client, dashboard_url, "DELETE",
                    f"{api_base}profiles/{name}",
                    timeout=30.0,
                )
                if resp.status_code == 404:
                    return {"ok": False, "error": f"profile '{name}' not found"}
                if resp.status_code >= 400:
                    detail = resp.text.strip() or resp.reason_phrase
                    return {"ok": False, "error": detail[:300]}
                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                self._invalidate_hermes_profiles_cache()
                return {"ok": True, "name": name, "path": data.get("path", "")}
        except Exception as e:
            return {"ok": False, "error": f"failed to delete profile: {e}"}

