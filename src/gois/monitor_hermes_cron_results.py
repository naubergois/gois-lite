"""Hermes cron job results and source viewer."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from .hermes_cron import (
    get_cron_job_result,
    hermes_home_from_jobs_path,
    resolve_agent_log_path,
    resolve_cron_job_ref,
)

log = logging.getLogger(__name__)

_CRON_SOURCE_QUOTED_PATH_RE = re.compile(
    r"[\"']([^\"'\n]+?\.(?:py|mjs|cjs|js|ts|tsx|sh|bash|zsh|ps1|rb|go|rs|java|kt|php))(?=[\"'])",
    re.IGNORECASE,
)
_CRON_SOURCE_BARE_PATH_RE = re.compile(
    r"(?<!://)(?:~|\.{1,2}|/)?[A-Za-z0-9_./-]+\.(?:py|mjs|cjs|js|ts|tsx|sh|bash|zsh|ps1|rb|go|rs|java|kt|php)",
    re.IGNORECASE,
)


class MonitorHermesCronResultsMixin:
    def handle_hermes_cron_result(
        self,
        job_id: str,
        *,
        run_file: Optional[str] = None,
    ) -> dict:
        """Return the saved agent response for one cron job run."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        jobs_path = self._hermes_cron_jobs_path()
        resolved = resolve_cron_job_ref(job_id, jobs_path)
        if not resolved.get("ok"):
            out = {"ok": False, "error": resolved.get("error")}
            if resolved.get("matches"):
                out["matches"] = resolved["matches"]
            return out
        canon_id = str(resolved["job_id"])
        job = resolved.get("job") if isinstance(resolved.get("job"), dict) else {}
        workdir = job.get("workdir")
        jobs_path = self._hermes_cron_jobs_path()
        home = hermes_home_from_jobs_path(jobs_path)
        return get_cron_job_result(
            canon_id,
            self._hermes_cron_output_path(),
            run_file=run_file,
            workdir=str(workdir) if workdir else None,
            job=job,
            agent_log_path=resolve_agent_log_path(
                self.cfg.hermes.log_paths if self.cfg.hermes else None
            ),
            errors_log_path=home / "logs" / "errors.log",
        )

    def _extract_cron_source_candidates(self, prompt: str) -> list[str]:
        text = str(prompt or "")
        out: list[str] = []
        seen: set[str] = set()

        for match in _CRON_SOURCE_QUOTED_PATH_RE.finditer(text):
            value = str(match.group(1) or "").strip()
            if not value or "://" in value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(value)

        for match in _CRON_SOURCE_BARE_PATH_RE.finditer(text):
            value = str(match.group(0) or "").strip()
            if not value or "://" in value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(value)

        return out

    def _resolve_cron_source_path(self, candidate: str, *, workdir: Optional[str]) -> Optional[Path]:
        raw = str(candidate or "").strip().strip('"').strip("'")
        if not raw:
            return None

        options: list[Path] = []
        cand = Path(raw).expanduser()
        if cand.is_absolute():
            options.append(cand)
        else:
            if workdir:
                try:
                    options.append((Path(workdir).expanduser() / cand).resolve())
                except OSError:
                    pass
            try:
                options.append((Path.cwd() / cand).resolve())
            except OSError:
                pass

        for path in options:
            try:
                if path.is_file():
                    return path.resolve()
            except OSError:
                continue
        return None

    def _resolve_cron_profile_source_path(self, job: dict[str, Any]) -> Optional[Path]:
        source_jobs_path = str(job.get("source_jobs_path") or "").strip()
        source_profile = str(job.get("source_profile") or "").strip()

        roots: list[Path] = []
        if source_jobs_path:
            try:
                jobs_path = Path(source_jobs_path).expanduser().resolve()
                # profiles/<slug>/cron/jobs.json -> profiles/<slug>
                roots.append(jobs_path.parent.parent)
            except OSError:
                pass

        if source_profile and source_profile != "default":
            try:
                home = hermes_home_from_jobs_path(self._hermes_cron_jobs_path())
                roots.append((home / "profiles" / source_profile).resolve())
            except OSError:
                pass

        candidates = (
            "SOUL.md",
            "AGENTS.md",
            "SYSTEM.md",
            "PROMPT.md",
            "prompt.md",
            "README.md",
        )
        seen: set[Path] = set()
        for root in roots:
            if root in seen:
                continue
            seen.add(root)
            for name in candidates:
                path = root / name
                try:
                    if path.is_file():
                        return path.resolve()
                except OSError:
                    continue
        return None

    def handle_hermes_cron_source(self, job_id: str) -> dict[str, Any]:
        """Return source code inferred from a Hermes cron job prompt."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}

        resolved = self._resolve_hermes_cron_job_context(job_id)
        if not resolved.get("ok"):
            out: dict[str, Any] = {"ok": False, "error": resolved.get("error")}
            if resolved.get("matches"):
                out["matches"] = resolved["matches"]
            return out

        job = resolved.get("job") if isinstance(resolved.get("job"), dict) else {}
        canon_id = str(resolved.get("job_id") or job_id)
        prompt = str(job.get("prompt") or "")
        workdir = str(job.get("workdir") or "").strip() or None
        source_profile = str(job.get("source_profile") or "").strip() or None
        source_jobs_path = str(job.get("source_jobs_path") or "").strip() or None

        candidates = self._extract_cron_source_candidates(prompt)
        source_path: Optional[Path] = None
        matched_candidate = ""
        for candidate in candidates:
            found = self._resolve_cron_source_path(candidate, workdir=workdir)
            if found is None:
                continue
            source_path = found
            matched_candidate = candidate
            break

        if source_path is None:
            profile_source = self._resolve_cron_profile_source_path(job)
            if profile_source is not None:
                source_path = profile_source
                label = str(source_profile or "default")
                matched_candidate = f"profile:{label}/{profile_source.name}"

        if source_path is None:
            fallback_prompt = prompt.strip()
            if fallback_prompt:
                max_chars = 180_000
                truncated = len(fallback_prompt) > max_chars
                return {
                    "ok": True,
                    "job_id": canon_id,
                    "job_name": str(job.get("name") or canon_id),
                    "workdir": workdir,
                    "source_profile": source_profile,
                    "source_jobs_path": source_jobs_path,
                    "source_path": "<cron-prompt>",
                    "language": "markdown",
                    "matched_candidate": "prompt",
                    "truncated": truncated,
                    "content": fallback_prompt[:max_chars] if truncated else fallback_prompt,
                }
            return {
                "ok": False,
                "error": "não foi possível inferir o arquivo-fonte a partir do prompt do cron",
                "job_id": canon_id,
                "job_name": str(job.get("name") or canon_id),
                "workdir": workdir,
                "source_profile": source_profile,
                "source_jobs_path": source_jobs_path,
                "candidates": candidates,
            }

        try:
            raw = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {
                "ok": False,
                "error": f"falha ao ler arquivo-fonte: {exc}",
                "job_id": canon_id,
                "source_path": str(source_path),
            }

        max_chars = 180_000
        truncated = len(raw) > max_chars
        content = raw[:max_chars] if truncated else raw

        ext = source_path.suffix.lower()
        lang_map = {
            ".py": "python",
            ".mjs": "javascript",
            ".cjs": "javascript",
            ".js": "javascript",
            ".ts": "typescript",
            ".tsx": "tsx",
            ".sh": "bash",
            ".bash": "bash",
            ".zsh": "zsh",
            ".ps1": "powershell",
            ".rb": "ruby",
            ".go": "go",
            ".rs": "rust",
            ".java": "java",
            ".kt": "kotlin",
            ".php": "php",
        }

        return {
            "ok": True,
            "job_id": canon_id,
            "job_name": str(job.get("name") or canon_id),
            "workdir": workdir,
            "source_profile": source_profile,
            "source_jobs_path": source_jobs_path,
            "source_path": str(source_path),
            "language": lang_map.get(ext, "text"),
            "matched_candidate": matched_candidate,
            "truncated": truncated,
            "content": content,
        }

