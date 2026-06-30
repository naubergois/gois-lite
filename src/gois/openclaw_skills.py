"""List OpenClaw skills for the dashboard chat."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .openclaw_chat import QclawRuntime, _TOOL_TO_SKILL_MAP, list_openclaw_agents
from .user_areas import classify_skill

log = logging.getLogger(__name__)

_skill_cache: Optional[list[dict[str, Any]]] = None
_skill_cache_key: Optional[str] = None
_skill_cache_ts: float = 0.0
_SKILL_CACHE_TTL = 60.0  # seconds

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUNDLED_SKILLS_DIR = _REPO_ROOT / "skills"
_QCLAW_CHAT_AUTO_ASSIGN = (
    "notion-calendar-sync",
    "google-calendar-crud-sync",
    "viagem-planejamento",
    "viagem-index",
    "qclaw-travel-stack",
    "qclaw-swarm-topology",
    "qclaw-agent-evaluate",
    "qclaw-agent-fix",
)

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_NAME = re.compile(r"^name:\s*[\"']?([^\"'\n]+)", re.MULTILINE)
_DESC = re.compile(
    r"^description:\s*[\"']?(.+?)[\"']?\s*$",
    re.MULTILINE,
)


def _description_from_body(text: str, *, max_chars: int = 220) -> str:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith("```"):
            continue
        lines.append(line)
        if len(" ".join(lines)) >= max_chars:
            break
    if not lines:
        return ""
    merged = " ".join(lines)
    if len(merged) > max_chars:
        return merged[: max_chars - 1].rstrip() + "…"
    return merged


def _parse_skill_md(path: Path) -> Optional[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _FRONTMATTER.match(text)
    if m:
        block = m.group(1)
        nm = _NAME.search(block)
        name = nm.group(1).strip() if nm else path.parent.name
        desc_m = _DESC.search(block)
        description = desc_m.group(1).strip() if desc_m else ""
        if description.startswith('"') and description.endswith('"'):
            description = description[1:-1]
        if not description:
            body = text[m.end() :].strip()
            description = _description_from_body(body)
    else:
        # Many community skills ship without YAML frontmatter.
        # Keep them visible using folder name + first body sentence.
        name = path.parent.name
        description = _description_from_body(text)
    return {"name": name, "description": description}


def _load_openclaw_config(config_path: Path) -> dict:
    if not config_path.is_file():
        return {}
    try:
        data = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _expand_path(raw: str) -> Path:
    return Path(raw).expanduser()


def _skill_dirs_from_config(
    cfg: dict,
    runtime: QclawRuntime,
    *,
    gois_extra_dirs: Optional[list[str]] = None,
) -> list[Path]:
    dirs: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            return
        seen.add(key)
        dirs.append(p)

    skills_cfg = cfg.get("skills") if isinstance(cfg.get("skills"), dict) else {}
    load_cfg = skills_cfg.get("load") if isinstance(skills_cfg.get("load"), dict) else {}
    extra = load_cfg.get("extraDirs")
    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, str) and item.strip():
                add(_expand_path(item.strip()))
    if gois_extra_dirs:
        for item in gois_extra_dirs:
            if isinstance(item, str) and item.strip():
                add(_expand_path(item.strip()))

    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    defaults = agents.get("defaults") if isinstance(agents.get("defaults"), dict) else {}
    default_ws = defaults.get("workspace")
    if isinstance(default_ws, str) and default_ws.strip():
        add(_expand_path(default_ws.strip()) / "skills")

    agent_list = agents.get("list") if isinstance(agents.get("list"), list) else []
    for agent in agent_list:
        if not isinstance(agent, dict):
            continue
        ws = agent.get("workspace")
        if isinstance(ws, str) and ws.strip():
            add(_expand_path(ws.strip()) / "skills")

    plugins = runtime.state_dir / "plugins"
    if plugins.is_dir():
        add(plugins)

    add(runtime.state_dir / "skills")
    add(runtime.state_dir / "workspace" / "skills")
    add(_BUNDLED_SKILLS_DIR)
    return dirs


def resolve_skill_scan_dirs(
    runtime: QclawRuntime,
    *,
    gois_extra_dirs: Optional[list[str]] = None,
) -> list[Path]:
    """Skill roots for chat/MCP: OpenClaw config + QClaw extras + bundled."""
    cfg = _load_openclaw_config(runtime.config_path)
    return _skill_dirs_from_config(cfg, runtime, gois_extra_dirs=gois_extra_dirs)


def _agent_skill_names(cfg: dict, agent_id: str) -> set[str]:
    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    agent_list = agents.get("list") if isinstance(agents.get("list"), list) else []
    for agent in agent_list:
        if not isinstance(agent, dict):
            continue
        aid = agent.get("id")
        if aid != agent_id:
            continue
        raw = agent.get("skills")
        if isinstance(raw, list):
            return {str(s).strip() for s in raw if str(s).strip()}
        return set()
    return set()


def _skill_entry_flags(cfg: dict) -> dict[str, bool]:
    skills_cfg = cfg.get("skills") if isinstance(cfg.get("skills"), dict) else {}
    entries = skills_cfg.get("entries") if isinstance(skills_cfg.get("entries"), dict) else {}
    out: dict[str, bool] = {}
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        enabled = entry.get("enabled")
        if isinstance(enabled, bool):
            out[str(name)] = enabled
    return out


def _scan_skill_dirs(dirs: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for root in dirs:
        if not root.is_dir():
            continue
        source = str(root)
        for skill_md in sorted(root.rglob("SKILL.md")):
            parsed = _parse_skill_md(skill_md)
            if not parsed:
                continue
            name = parsed["name"]
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                rel = str(skill_md.parent.relative_to(root))
            except ValueError:
                rel = skill_md.parent.name
            rows.append(
                {
                    "name": name,
                    "slug": skill_md.parent.name,
                    "description": parsed.get("description") or "",
                    "source": source,
                    "path": rel,
                }
            )
    rows.sort(key=lambda r: (r.get("name") or "").lower())
    return rows


def scan_all_skill_rows(dirs: list[Path]) -> list[dict[str, Any]]:
    """Bundled/Hermes SKILL.md dirs plus .kiro/skills/*.md."""
    global _skill_cache, _skill_cache_key, _skill_cache_ts

    # Build cache key from dir paths + mtimes
    cache_parts: list[str] = []
    for d in dirs:
        try:
            if d.is_dir():
                cache_parts.append(f"{d}:{d.stat().st_mtime_ns}")
        except OSError:
            pass
    key = "|".join(cache_parts)
    now = time.monotonic()

    if (
        _skill_cache is not None
        and _skill_cache_key == key
        and (now - _skill_cache_ts) < _SKILL_CACHE_TTL
    ):
        return _skill_cache

    from .kiro_skills import merge_skill_rows, scan_kiro_skill_rows

    bundled = _scan_skill_dirs(dirs)
    kiro = scan_kiro_skill_rows()
    result = merge_skill_rows(bundled, kiro)

    _skill_cache = result
    _skill_cache_key = key
    _skill_cache_ts = now
    return result


def _skill_md_path(row: dict[str, Any]) -> Optional[Path]:
    kiro_file = row.get("kiro_file")
    if isinstance(kiro_file, str) and kiro_file.strip():
        path = Path(kiro_file)
        return path if path.is_file() else None
    source = row.get("source")
    rel = row.get("path")
    if not isinstance(source, str) or not isinstance(rel, str):
        return None
    path = Path(source) / rel / "SKILL.md"
    return path if path.is_file() else None


def is_qclaw_chat_skill(name: str, slug: str = "") -> bool:
    """True for bundled qclaw-chat skills and related auto-assign slugs."""
    for key in (name, slug):
        val = (key or "").strip()
        if not val:
            continue
        if val.startswith("qclaw-chat-"):
            return True
        if val in _QCLAW_CHAT_AUTO_ASSIGN:
            return True
    return False


def _is_qclaw_chat_skill(name: str, slug: str) -> bool:
    return is_qclaw_chat_skill(name, slug)


def list_bundled_qclaw_chat_skills() -> list[dict[str, Any]]:
    """Scan repo ``skills/`` for qclaw-chat SKILL.md entries (no runtime)."""
    if not _BUNDLED_SKILLS_DIR.is_dir():
        return []
    rows = _scan_skill_dirs([_BUNDLED_SKILLS_DIR])
    return [
        row
        for row in rows
        if is_qclaw_chat_skill(
            str(row.get("name") or ""),
            str(row.get("slug") or ""),
        )
    ]


def list_openclaw_skills_for_swarm_catalog(
    runtime: QclawRuntime,
) -> list[dict[str, Any]]:
    """OpenClaw skills for swarm agent profile pickers (excludes qclaw-chat)."""
    cfg = _load_openclaw_config(runtime.config_path)
    dirs = _skill_dirs_from_config(cfg, runtime)
    rows = _scan_skill_dirs(dirs)
    out: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        slug = str(row.get("slug") or "").strip()
        if not name or is_qclaw_chat_skill(name, slug):
            continue
        out.append(
            {
                "name": name,
                "description": row.get("description") or "",
                "category": "openclaw",
                "source": "openclaw",
                "slug": slug,
                "path": row.get("path") or "",
            }
        )
    out.sort(key=lambda r: (r.get("name") or "").lower())
    return out


def _auto_assign_qclaw_chat_skills(rows: list[dict[str, Any]], *, agent_id: str) -> None:
    """Marca skills qclaw-chat do repo como assigned/enabled no agente main."""
    if agent_id != "main":
        return
    bundled = str(_BUNDLED_SKILLS_DIR.resolve())
    for row in rows:
        if row.get("source") != bundled:
            continue
        name = str(row.get("name") or "")
        slug = str(row.get("slug") or "")
        if row.get("enabled") is False:
            continue
        if _is_qclaw_chat_skill(name, slug):
            row["assigned"] = True
            row["enabled"] = True


_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "o", "os", "as", "um", "uma", "de", "do", "da", "dos", "das", "e",
        "ou", "para", "por", "com", "sem", "no", "na", "nos", "nas", "em", "que",
        "se", "ao", "aos", "the", "to", "of", "and", "or", "for", "with", "in",
        "on", "me", "my", "meu", "minha", "qual", "como", "quero", "preciso",
        "favor", "por favor", "pode", "fazer", "criar", "gerar",
    }
)

_WORD_RE = re.compile(r"[a-zà-ÿ0-9]+", re.IGNORECASE)


def _tokenize_for_relevance(text: str) -> set[str]:
    return {
        tok
        for tok in _WORD_RE.findall((text or "").lower())
        if len(tok) >= 3 and tok not in _STOPWORDS
    }


def _skill_relevance_score(row: dict[str, Any], query_tokens: set[str]) -> int:
    """Keyword overlap between the user message and a skill's name/slug/desc."""
    if not query_tokens:
        return 0
    name = str(row.get("name") or "")
    slug = str(row.get("slug") or "")
    desc = str(row.get("description") or "")
    name_tokens = _tokenize_for_relevance(f"{name} {slug}")
    desc_tokens = _tokenize_for_relevance(desc)
    # Name/slug matches weigh more than description matches.
    return 3 * len(query_tokens & name_tokens) + len(query_tokens & desc_tokens)


def _pick_skills_for_chat(
    rows: list[dict[str, Any]],
    *,
    max_items: int,
    user_text: str = "",
) -> list[dict[str, Any]]:
    assigned = [r for r in rows if r.get("assigned")]
    enabled = [r for r in rows if r.get("enabled") and not r.get("assigned")]
    query_tokens = _tokenize_for_relevance(user_text)

    # qclaw-chat primeiro no prompt (corpo SKILL.md limitado por budget).
    # Quando há mensagem, skills relevantes à intenção sobem dentro de cada grupo.
    def _sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
        name = str(row.get("name") or "")
        slug = str(row.get("slug") or "")
        pri = 0 if _is_qclaw_chat_skill(name, slug) else 1
        rel = _skill_relevance_score(row, query_tokens)
        return (pri, -rel, name.lower())

    assigned = sorted(assigned, key=_sort_key)
    enabled = sorted(enabled, key=_sort_key)
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    limit = max_items if max_items and max_items > 0 else (len(rows) + 1)
    for row in assigned + enabled:
        name = str(row.get("name") or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        picked.append(row)
        if len(picked) >= limit:
            break
    return picked


def format_openclaw_skills_for_chat(
    rows: list[dict[str, Any]],
    *,
    max_items: int = 0,
    max_body_chars: int = 12000,
    user_text: str = "",
) -> str:
    """Build chat prompt block with full catalog + selected SKILL.md bodies.

    When ``user_text`` is provided and ``max_items`` caps the catalog, skills
    whose name/description match the message are ranked first so the limited
    slots are spent on the most relevant skills.
    """
    picked = _pick_skills_for_chat(rows, max_items=max_items, user_text=user_text)
    if not picked:
        return "(nenhuma skill OpenClaw encontrada)"

    catalog_lines = [
        "## Skills disponíveis",
        "(Lembre-se: ao usar uma skill, coloque `<!--skills:nome-da-skill-->` na primeira linha da resposta.)",
    ]
    for row in picked:
        name = row.get("name") or "?"
        desc = (row.get("description") or "").strip()
        if desc and len(desc) > 140:
            desc = desc[:137] + "…"
        catalog_lines.append(f"- {name}: {desc}" if desc else f"- {name}")
    catalog_block = "\n".join(catalog_lines)

    if max_body_chars == 0:
        return catalog_block

    parts: list[str] = []
    budget = max_body_chars if max_body_chars > 0 else 10**9
    for row in picked:
        name = row.get("name") or "?"
        desc = (row.get("description") or "").strip()
        header = f"### {name}"
        if desc:
            header += f"\n{desc}"
        skill_md = _skill_md_path(row)
        body = ""
        if skill_md:
            try:
                body = skill_md.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                body = ""
        block = header
        if body:
            block += f"\n\n{body}"
        if len(block) > budget:
            if budget < 80:
                break
            block = block[: budget - 1] + "…"
        parts.append(block)
        budget -= len(block) + 2
        if budget <= 0:
            break
    detailed = "\n\n".join(parts)
    if detailed:
        return catalog_block + "\n\n## Conteúdo SKILL.md\n\n" + detailed
    return catalog_block


class _SkillsCache:
    """In-memory TTL cache for the skills catalog to avoid repeated filesystem scans."""
    _TTL_SECONDS = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: Optional[list[dict[str, Any]]] = None
        self._agent_results: dict[str, dict[str, Any]] = {}
        self._mtimes: dict[str, float] = {}  # path -> mtime at last scan
        self._dirs_key: str = ""
        self._expires_at: float = 0.0

    def invalidate(self) -> None:
        with self._lock:
            self._rows = None
            self._agent_results.clear()
            self._mtimes.clear()
            self._expires_at = 0.0

    def _dirs_fingerprint(self, dirs: list[Path]) -> str:
        return "|".join(str(d) for d in dirs)

    def _check_mtimes(self) -> bool:
        """Fast check: compare mtime of known SKILL.md files without re-parsing."""
        if not self._mtimes:
            return False
        for path_str, old_mtime in self._mtimes.items():
            try:
                current = Path(path_str).stat().st_mtime
                if current != old_mtime:
                    return False
            except OSError:
                return False
        return True

    def _record_mtimes(self, dirs: list[Path]) -> None:
        mtimes: dict[str, float] = {}
        for d in dirs:
            if not d.is_dir():
                continue
            for skill_md in d.rglob("SKILL.md"):
                try:
                    mtimes[str(skill_md)] = skill_md.stat().st_mtime
                except OSError:
                    pass
        self._mtimes = mtimes

    def get(
        self,
        runtime: "QclawRuntime",
        *,
        agent_id: Optional[str] = None,
        gois_extra_dirs: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        resolved_agent = agent_id or "main"
        runtime_key = str(runtime.config_path)
        cache_key = f"{runtime_key}:{resolved_agent}"
        now = time.time()
        with self._lock:
            if (
                self._rows is not None
                and now < self._expires_at
                and cache_key in self._agent_results
            ):
                log.debug("skills cache HIT for agent=%s", resolved_agent)
                return self._agent_results[cache_key]
            # Fast mtime check: if nothing changed, renew TTL
            if self._rows is not None and self._check_mtimes():
                self._expires_at = now + self._TTL_SECONDS
                if cache_key in self._agent_results:
                    log.debug("skills cache RENEW (mtimes unchanged) for agent=%s", resolved_agent)
                    return self._agent_results[cache_key]
        # Full re-scan needed
        result = _list_openclaw_skills_uncached(
            runtime,
            agent_id=agent_id,
            gois_extra_dirs=gois_extra_dirs,
        )
        with self._lock:
            cfg = _load_openclaw_config(runtime.config_path)
            dirs = _skill_dirs_from_config(cfg, runtime, gois_extra_dirs=gois_extra_dirs)
            self._dirs_key = self._dirs_fingerprint(dirs)
            self._rows = result.get("skills") or []
            self._record_mtimes(dirs)
            self._agent_results[cache_key] = result
            self._expires_at = now + self._TTL_SECONDS
        log.debug("skills cache MISS — full re-scan for agent=%s (%d skills)", resolved_agent, len(self._rows))
        return result


_skills_cache = _SkillsCache()


def invalidate_skills_cache() -> None:
    """Force next skills access to re-scan the filesystem."""
    _skills_cache.invalidate()


def list_openclaw_skills(
    runtime: QclawRuntime,
    *,
    agent_id: Optional[str] = None,
    gois_extra_dirs: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Return OpenClaw skills catalog (cached with TTL)."""
    return _skills_cache.get(
        runtime,
        agent_id=agent_id,
        gois_extra_dirs=gois_extra_dirs,
    )


def _list_openclaw_skills_uncached(
    runtime: QclawRuntime,
    *,
    agent_id: Optional[str] = None,
    gois_extra_dirs: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Return OpenClaw skills catalog for /openclaw/skills."""
    cfg = _load_openclaw_config(runtime.config_path)
    dirs = _skill_dirs_from_config(cfg, runtime, gois_extra_dirs=gois_extra_dirs)
    rows = scan_all_skill_rows(dirs)
    entry_flags = _skill_entry_flags(cfg)
    resolved_agent = agent_id or "main"
    agent_skills = _agent_skill_names(cfg, resolved_agent)

    for row in rows:
        name = row["name"]
        slug = row.get("slug") or name
        enabled_entry = entry_flags.get(name)
        if enabled_entry is None:
            enabled_entry = entry_flags.get(slug)
        assigned = (
            name in agent_skills
            or slug in agent_skills
            or any(s.lower() == name.lower() for s in agent_skills)
        )
        if enabled_entry is False:
            row["enabled"] = False
        elif assigned:
            row["enabled"] = True
            row["assigned"] = True
        else:
            row["enabled"] = enabled_entry if enabled_entry is not None else True
        row["user_area"] = classify_skill(
            name=str(name),
            slug=str(slug),
            description=str(row.get("description") or ""),
        )

    _auto_assign_qclaw_chat_skills(rows, agent_id=resolved_agent)

    enabled_count = sum(1 for r in rows if r.get("enabled"))
    assigned_count = sum(1 for r in rows if r.get("assigned"))

    agents = list_openclaw_agents(runtime)
    if resolved_agent not in agents:
        agents = [resolved_agent, *agents]

    return {
        "ok": True,
        "agentId": resolved_agent,
        "agents": agents,
        "skills": rows,
        "count": len(rows),
        "enabled_count": enabled_count,
        "assigned_count": assigned_count,
        "skill_dirs": [str(d) for d in dirs if d.is_dir()],
        "control_url": runtime.control_url,
        "tool_skill_map": _TOOL_TO_SKILL_MAP,
    }
