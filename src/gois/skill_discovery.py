"""Daily scan for new OpenClaw / Hermes skills and user-facing suggestions."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from .config import SkillDiscoveryConfig
from .hermes_skills import DEFAULT_DEV_SKILL_SLUGS, list_development_skills
from .openclaw_chat import resolve_qclaw_runtime
from .openclaw_skills import _scan_skill_dirs, list_openclaw_skills

from .runtime_state import load_json, save_json

log = logging.getLogger(__name__)

_SKILL_SUGGESTIONS_KEY = "skill_suggestions:state"

_REPO_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


def _norm(name: str) -> str:
    return str(name or "").strip().lower().replace(" ", "-")


def _bundled_repo_skills_dir(cfg: SkillDiscoveryConfig) -> Path:
    raw = (cfg.bundled_skills_dir or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _REPO_SKILLS_DIR


def _state_path(cfg: SkillDiscoveryConfig) -> Path:
    return Path(cfg.state_path).expanduser()


def load_state(cfg: SkillDiscoveryConfig) -> dict[str, Any]:
    path = _state_path(cfg)
    data = load_json(_SKILL_SUGGESTIONS_KEY, path)
    if data is None:
        return {"suggestions": [], "last_scan_ts": 0.0, "last_notify_ts": 0.0}
    if not isinstance(data, dict):
        return {"suggestions": [], "last_scan_ts": 0.0, "last_notify_ts": 0.0}
    if not isinstance(data.get("suggestions"), list):
        data["suggestions"] = []
    return data


def save_state(cfg: SkillDiscoveryConfig, state: dict[str, Any]) -> None:
    save_json(_SKILL_SUGGESTIONS_KEY, state, _state_path(cfg))


def collect_installed_skill_names(*, agent_id: str = "main") -> set[str]:
    """Union of OpenClaw + Hermes skill names/slugs (normalized)."""
    names: set[str] = set()
    try:
        runtime = resolve_qclaw_runtime()
        oc = list_openclaw_skills(runtime, agent_id=agent_id)
        for row in oc.get("skills") or []:
            if isinstance(row, dict):
                for key in ("name", "slug"):
                    val = _norm(str(row.get(key) or ""))
                    if val:
                        names.add(val)
    except Exception as e:
        log.debug("openclaw skills for discovery: %s", e)

    try:
        hs = list_development_skills(include_cli_fallback=True)
        for row in hs.get("skills") or []:
            if isinstance(row, dict):
                val = _norm(str(row.get("name") or ""))
                if val:
                    names.add(val)
    except Exception as e:
        log.debug("hermes skills for discovery: %s", e)
    return names


def _search_clawhub(
    *,
    base_url: str,
    query: str,
    limit: int,
    timeout_seconds: float,
    fetch_json: Optional[Callable[..., dict]] = None,
) -> list[dict[str, Any]]:
    if fetch_json is not None:
        data = fetch_json(base_url=base_url, query=query, limit=limit)
        rows = data.get("results") if isinstance(data, dict) else None
        return rows if isinstance(rows, list) else []

    q = urllib.parse.urlencode({"q": query.strip() or "*", "limit": str(max(1, limit))})
    url = f"{base_url.rstrip('/')}/api/v1/search?{q}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        log.warning("clawhub search failed q=%r: %s", query, e)
        return []
    rows = payload.get("results") if isinstance(payload, dict) else None
    return rows if isinstance(rows, list) else []


def _suggestion_id(source: str, slug: str) -> str:
    return f"{source}:{_norm(slug)}"


def _append_suggestion(
    out: list[dict[str, Any]],
    *,
    source: str,
    slug: str,
    name: str,
    description: str,
    reason: str,
    install_hint: str = "",
    url: str = "",
    now: float,
) -> None:
    sid = _suggestion_id(source, slug)
    if any(s.get("id") == sid for s in out):
        return
    out.append(
        {
            "id": sid,
            "source": source,
            "slug": slug,
            "name": name or slug,
            "description": description.strip(),
            "reason": reason.strip(),
            "install_hint": install_hint.strip(),
            "url": url.strip(),
            "discovered_at": now,
            "dismissed": False,
            "notified": False,
        }
    )


def discover_skill_suggestions(
    cfg: SkillDiscoveryConfig,
    *,
    agent_id: str = "main",
    fetch_clawhub: Optional[Callable[..., dict]] = None,
) -> dict[str, Any]:
    """Scan sources and return new suggestions not yet in state."""
    now = time.time()
    installed = collect_installed_skill_names(agent_id=agent_id)
    state = load_state(cfg)
    known_ids = {
        str(s.get("id") or "")
        for s in state.get("suggestions") or []
        if isinstance(s, dict) and s.get("id")
    }
    pending: list[dict[str, Any]] = []

    if cfg.bundled_repo_skills:
        bundled_dir = _bundled_repo_skills_dir(cfg)
        if bundled_dir.is_dir():
            for row in _scan_skill_dirs([bundled_dir]):
                slug = _norm(str(row.get("slug") or row.get("name") or ""))
                if not slug or slug in installed:
                    continue
                sid = _suggestion_id("bundled", slug)
                if sid in known_ids:
                    continue
                _append_suggestion(
                    pending,
                    source="bundled",
                    slug=slug,
                    name=str(row.get("name") or slug),
                    description=str(row.get("description") or ""),
                    reason="Skill incluída no gois ainda não detectada no OpenClaw.",
                    install_hint=(
                        f"Copie ou linke `{bundled_dir / slug}` para o workspace OpenClaw "
                        "(pasta skills/) ou instale via ClawHub."
                    ),
                    now=now,
                )

    if cfg.hermes_recommended:
        for slug in DEFAULT_DEV_SKILL_SLUGS:
            norm = _norm(slug)
            if not norm or norm in installed:
                continue
            sid = _suggestion_id("hermes-recommended", norm)
            if sid in known_ids:
                continue
            _append_suggestion(
                pending,
                source="hermes-recommended",
                slug=norm,
                name=norm,
                description="Skill recomendada para agentes de código no Hermes.",
                reason="Recomendada pelo gois para workflows de desenvolvimento.",
                install_hint=f"Instale em ~/.hermes/skills/software-development/{norm}/SKILL.md",
                now=now,
            )

    if cfg.clawhub_enabled:
        queries: list[str] = []
        for q in cfg.clawhub_search_queries or []:
            text = str(q or "").strip()
            if text and text not in queries:
                queries.append(text)
        if cfg.clawhub_browse_all:
            queries.append("*")
        seen_slugs: set[str] = set()
        for query in queries:
            rows = _search_clawhub(
                base_url=cfg.clawhub_base_url,
                query=query,
                limit=cfg.clawhub_limit_per_query,
                timeout_seconds=cfg.clawhub_timeout_seconds,
                fetch_json=fetch_clawhub,
            )
            for row in rows:
                if not isinstance(row, dict):
                    continue
                slug = _norm(str(row.get("slug") or ""))
                if not slug or slug in installed or slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                sid = _suggestion_id("clawhub", slug)
                if sid in known_ids:
                    continue
                display = str(row.get("displayName") or slug)
                summary = str(row.get("summary") or row.get("description") or "")
                version = str(row.get("version") or "").strip()
                url = f"{cfg.clawhub_base_url.rstrip('/')}/openclaw/{slug}"
                reason = f"Encontrada no ClawHub (busca: {query!r})"
                if version:
                    reason += f", versão {version}"
                _append_suggestion(
                    pending,
                    source="clawhub",
                    slug=slug,
                    name=display,
                    description=summary,
                    reason=reason,
                    install_hint="No OpenClaw Control UI: Skills → ClawHub → Install, ou `openclaw skills install clawhub:" + slug + "`",
                    url=url,
                    now=now,
                )

    pending.sort(key=lambda s: (s.get("source") or "", s.get("name") or ""))
    if cfg.max_suggestions > 0:
        pending = pending[: cfg.max_suggestions]

    merged = list(state.get("suggestions") or [])
    merged.extend(pending)
    state["suggestions"] = merged
    state["last_scan_ts"] = now
    save_state(cfg, state)

    return {
        "ok": True,
        "new_count": len(pending),
        "pending_count": len(list_pending_suggestions(cfg)),
        "installed_count": len(installed),
        "new": pending,
        "last_scan_ts": now,
    }


def list_pending_suggestions(cfg: SkillDiscoveryConfig) -> list[dict[str, Any]]:
    rows = [
        s
        for s in load_state(cfg).get("suggestions") or []
        if isinstance(s, dict) and not s.get("dismissed")
    ]
    rows.sort(key=lambda s: float(s.get("discovered_at") or 0), reverse=True)
    if cfg.max_suggestions > 0:
        rows = rows[: cfg.max_suggestions]
    return rows


def dismiss_suggestion(cfg: SkillDiscoveryConfig, suggestion_id: str) -> dict[str, Any]:
    sid = str(suggestion_id or "").strip()
    if not sid:
        return {"ok": False, "error": "suggestion id required"}
    state = load_state(cfg)
    found = False
    for row in state.get("suggestions") or []:
        if isinstance(row, dict) and str(row.get("id") or "") == sid:
            row["dismissed"] = True
            found = True
            break
    if not found:
        return {"ok": False, "error": f"suggestion not found: {sid}"}
    save_state(cfg, state)
    return {"ok": True, "id": sid, "pending_count": len(list_pending_suggestions(cfg))}


def mark_suggestions_notified(cfg: SkillDiscoveryConfig, ids: list[str]) -> None:
    if not ids:
        return
    want = set(ids)
    state = load_state(cfg)
    for row in state.get("suggestions") or []:
        if isinstance(row, dict) and str(row.get("id") or "") in want:
            row["notified"] = True
    state["last_notify_ts"] = time.time()
    save_state(cfg, state)


def format_suggestions_whatsapp(rows: list[dict[str, Any]], *, max_items: int = 8) -> str:
    if not rows:
        return ""
    lines = ["🔎 **Novas skills sugeridas (gois)**", ""]
    for idx, row in enumerate(rows[:max_items], start=1):
        name = row.get("name") or row.get("slug") or "?"
        reason = (row.get("reason") or "").strip()
        hint = (row.get("install_hint") or "").strip()
        lines.append(f"{idx}. `{name}` — {reason}")
        if hint:
            lines.append(f"   → {hint}")
    extra = len(rows) - max_items
    if extra > 0:
        lines.append(f"\n… e mais {extra} no dashboard (/chat).")
    lines.append("\nAbra o painel Skills no chat para ver detalhes ou dispensar.")
    return "\n".join(lines)


def discovery_status(cfg: SkillDiscoveryConfig) -> dict[str, Any]:
    state = load_state(cfg)
    pending = list_pending_suggestions(cfg)
    return {
        "ok": True,
        "enabled": cfg.enabled,
        "interval_seconds": cfg.interval_seconds,
        "last_scan_ts": state.get("last_scan_ts") or 0.0,
        "last_notify_ts": state.get("last_notify_ts") or 0.0,
        "pending_count": len(pending),
        "pending": pending,
    }
