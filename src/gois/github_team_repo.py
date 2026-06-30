"""Create a private GitHub repository when a team is created."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_GH_TIMEOUT = 120.0
_REPO_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$", re.IGNORECASE)


def team_github_auto_create_enabled() -> bool:
    raw = os.environ.get("QCLAW_TEAM_GITHUB_AUTO_CREATE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def resolve_github_org(configured_org: Optional[str] = None) -> Optional[str]:
    org = (configured_org or os.environ.get("QCLAW_GITHUB_ORG") or "").strip()
    return org or None


def gh_auth_ok() -> bool:
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        log.debug("gh auth check failed: %s", exc)
        return False


def _run(cmd: list[str], *, cwd: Optional[Path] = None, timeout: float = _GH_TIMEOUT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _resolve_owner(owner: Optional[str]) -> str:
    if owner:
        return owner.strip()
    proc = _run(["gh", "api", "user", "--jq", ".login"], timeout=30)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "gh api user failed").strip())
    login = (proc.stdout or "").strip()
    if not login:
        raise RuntimeError("gh api user returned empty login")
    return login


def github_repo_slug(team_id: str, team_name: str = "") -> str:
    candidate = (team_id or "").strip().lower()
    if _REPO_NAME_RE.match(candidate):
        return candidate
    fallback = re.sub(r"[^a-z0-9]+", "-", (team_name or team_id).strip().lower()).strip("-")
    if fallback and _REPO_NAME_RE.match(fallback):
        return fallback[:100]
    return f"team-{candidate[:80] or 'workspace'}"


def _ensure_git_repo(workspace: Path, *, commit_message: str) -> None:
    git_dir = workspace / ".git"
    if not git_dir.exists():
        init = _run(["git", "init", "-b", "main"], cwd=workspace, timeout=30)
        if init.returncode != 0:
            raise RuntimeError((init.stderr or init.stdout or "git init failed").strip())
        for key, value in (
            ("user.name", "QClaw Monitor"),
            ("user.email", "qclaw-monitor@users.noreply.github.com"),
        ):
            cfg = _run(["git", "config", key, value], cwd=workspace, timeout=15)
            if cfg.returncode != 0:
                raise RuntimeError((cfg.stderr or cfg.stdout or f"git config {key} failed").strip())
    status = _run(["git", "status", "--porcelain"], cwd=workspace, timeout=30)
    if status.returncode != 0:
        raise RuntimeError((status.stderr or status.stdout or "git status failed").strip())
    if (status.stdout or "").strip():
        add = _run(["git", "add", "-A"], cwd=workspace, timeout=60)
        if add.returncode != 0:
            raise RuntimeError((add.stderr or add.stdout or "git add failed").strip())
        commit = _run(["git", "commit", "-m", commit_message], cwd=workspace, timeout=60)
        if commit.returncode != 0:
            raise RuntimeError((commit.stderr or commit.stdout or "git commit failed").strip())


def _repo_view_url(owner: str, repo_name: str) -> Optional[str]:
    proc = _run(
        ["gh", "repo", "view", f"{owner}/{repo_name}", "--json", "url", "--jq", ".url"],
        timeout=30,
    )
    if proc.returncode != 0:
        return None
    url = (proc.stdout or "").strip()
    return url or None


def create_private_team_repo(
    *,
    team_id: str,
    team_name: str,
    description: str = "",
    workspace_path: Path,
    owner: Optional[str] = None,
    branch: str = "main",
) -> dict[str, Any]:
    """Create a private GitHub repo for a team and push the workspace contents."""
    workspace = workspace_path.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"workspace do time não encontrado: {workspace}")

    gh_owner = _resolve_owner(owner)
    repo_name = github_repo_slug(team_id, team_name)
    full_name = f"{gh_owner}/{repo_name}"
    desc = (description or f"Repositório do time {team_name} (QClaw)").strip()[:350]

    _ensure_git_repo(
        workspace,
        commit_message=f"chore: initial workspace for team {team_name}",
    )

    existing_url = _repo_view_url(gh_owner, repo_name)
    if existing_url:
        log.info("github repo already exists for team %s: %s", team_id, existing_url)
        return {
            "url": existing_url,
            "owner": gh_owner,
            "name": repo_name,
            "branch": branch or "main",
            "created": False,
        }

    create_cmd = [
        "gh",
        "repo",
        "create",
        full_name,
        "--private",
        f"--description={desc}",
        "--source=.",
        "--remote=origin",
        "--push",
    ]
    proc = _run(create_cmd, cwd=workspace, timeout=_GH_TIMEOUT)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "gh repo create failed").strip()
        raise RuntimeError(err)

    url = _repo_view_url(gh_owner, repo_name)
    if not url:
        url = f"https://github.com/{full_name}"
    log.info("created private github repo for team %s: %s", team_id, url)
    return {
        "url": url,
        "owner": gh_owner,
        "name": repo_name,
        "branch": branch or "main",
        "created": True,
    }
