"""List Hermes development skills for the dashboard chat."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from .hermes_cron import resolve_hermes_bin
from .local_paths import hermes_home

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_NAME = re.compile(r"^name:\s*[\"']?([^\"'\n]+)", re.MULTILINE)
_DESC = re.compile(
    r"^description:\s*[\"']?(.+?)[\"']?\s*$",
    re.MULTILINE,
)

DEFAULT_DEV_CATEGORIES = ("software-development",)
DEFAULT_DEV_SKILL_SLUGS = (
    "qclaw-mcp-skills-index",
    "plan",
    "writing-plans",
    "test-driven-development",
    "systematic-debugging",
    "requesting-code-review",
    "subagent-driven-development",
)


def _parse_skill_md(path: Path) -> Optional[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _FRONTMATTER.match(text)
    if not m:
        return None
    block = m.group(1)
    nm = _NAME.search(block)
    if not nm:
        return None
    name = nm.group(1).strip()
    desc_m = _DESC.search(block)
    description = desc_m.group(1).strip() if desc_m else ""
    if description.startswith('"') and description.endswith('"'):
        description = description[1:-1]
    return {"name": name, "description": description}


def _skills_root() -> Path:
    return hermes_home() / "skills"


def list_skills_from_tree(
    *,
    categories: Optional[list[str]] = None,
    skills_root: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Scan ~/.hermes/skills/<category>/**/SKILL.md for installed skills."""
    root = skills_root or _skills_root()
    cats = categories or list(DEFAULT_DEV_CATEGORIES)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for cat in cats:
        cat_dir = root / cat
        if not cat_dir.is_dir():
            continue
        for skill_md in sorted(cat_dir.rglob("SKILL.md")):
            parsed = _parse_skill_md(skill_md)
            if not parsed or parsed["name"] in seen:
                continue
            seen.add(parsed["name"])
            rows.append({
                "name": parsed["name"],
                "description": parsed["description"],
                "category": cat,
                "path": str(skill_md.parent.relative_to(root)),
            })
    rows.sort(key=lambda r: (r.get("category") or "", r.get("name") or ""))
    return rows


def list_skills_via_cli(timeout: float = 30.0) -> list[dict[str, Any]]:
    """Fallback: parse `hermes skills list` table (names may be truncated)."""
    try:
        proc = subprocess.run(
            [resolve_hermes_bin(), "skills", "list"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        if "│" not in line or "Name" in line or "━━" in line:
            continue
        parts = [p.strip() for p in line.split("│") if p.strip()]
        if len(parts) < 2:
            continue
        name = parts[0].rstrip("…").strip()
        category = parts[1] if len(parts) > 1 else ""
        if not name or name.lower() == "name":
            continue
        rows.append({
            "name": name,
            "description": "",
            "category": category,
            "truncated": name.endswith("…"),
        })
    return rows


def _swarm_skill_sort_key(row: dict[str, Any]) -> tuple[int, str, str]:
    pri = {"qclaw-chat": 0, "openclaw": 1, "hermes": 2}.get(
        str(row.get("source") or ""),
        3,
    )
    return (pri, str(row.get("category") or ""), str(row.get("name") or ""))


def list_swarm_skills_catalog(
    *,
    categories: Optional[list[str]] = None,
    include_qclaw_chat: bool = True,
    include_openclaw: bool = True,
    runtime: Optional[Any] = None,
) -> dict[str, Any]:
    """Hermes dev + qclaw-chat + OpenClaw skills for swarm agent profile pickers."""
    dev = list_development_skills(categories=categories)
    skills: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in dev.get("skills") or []:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        skills.append({**row, "source": "hermes"})

    if include_qclaw_chat:
        from .openclaw_skills import list_bundled_qclaw_chat_skills

        for row in list_bundled_qclaw_chat_skills():
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            skills.append(
                {
                    "name": name,
                    "description": row.get("description") or "",
                    "category": "qclaw-chat",
                    "source": "qclaw-chat",
                    "path": row.get("path") or "",
                }
            )

    if include_openclaw and runtime is not None:
        from .openclaw_skills import list_openclaw_skills_for_swarm_catalog

        for row in list_openclaw_skills_for_swarm_catalog(runtime):
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            skills.append(row)

    skills.sort(key=_swarm_skill_sort_key)
    cats = list(categories or DEFAULT_DEV_CATEGORIES)
    if include_qclaw_chat and "qclaw-chat" not in cats:
        cats.append("qclaw-chat")
    if include_openclaw and "openclaw" not in cats:
        cats.append("openclaw")
    return {
        "ok": True,
        "source": dev.get("source"),
        "categories": cats,
        "skills": skills,
        "recommended": list(dev.get("recommended") or DEFAULT_DEV_SKILL_SLUGS),
        "qclaw_chat_count": sum(1 for r in skills if r.get("source") == "qclaw-chat"),
        "openclaw_count": sum(1 for r in skills if r.get("source") == "openclaw"),
    }


def swarm_known_skill_names(
    create_cfg: Any,
    *,
    include_qclaw_chat: bool = True,
    include_openclaw: bool = True,
    runtime: Optional[Any] = None,
) -> set[str]:
    """All skill slugs valid for swarm agents (Hermes + qclaw-chat + OpenClaw)."""
    catalog = list_swarm_skills_catalog(
        categories=getattr(create_cfg, "skill_categories", None),
        include_qclaw_chat=include_qclaw_chat,
        include_openclaw=include_openclaw,
        runtime=runtime,
    )
    return {
        str(r["name"])
        for r in (catalog.get("skills") or [])
        if r.get("name")
    }


def list_development_skills(
    *,
    categories: Optional[list[str]] = None,
    include_cli_fallback: bool = True,
) -> dict[str, Any]:
    """Return dev skills catalog for /hermes/skills and agent-create prompts."""
    tree_rows = list_skills_from_tree(categories=categories)
    if tree_rows:
        return {
            "ok": True,
            "source": "tree",
            "categories": list(categories or DEFAULT_DEV_CATEGORIES),
            "skills": tree_rows,
            "recommended": list(DEFAULT_DEV_SKILL_SLUGS),
        }
    if include_cli_fallback:
        cli_rows = list_skills_via_cli()
        if cli_rows:
            dev = [
                r for r in cli_rows
                if "software-development" in (r.get("category") or "").lower()
            ]
            return {
                "ok": True,
                "source": "cli",
                "categories": list(categories or DEFAULT_DEV_CATEGORIES),
                "skills": dev or cli_rows,
                "recommended": list(DEFAULT_DEV_SKILL_SLUGS),
            }
    return {
        "ok": True,
        "source": "empty",
        "categories": list(categories or DEFAULT_DEV_CATEGORIES),
        "skills": [],
        "recommended": list(DEFAULT_DEV_SKILL_SLUGS),
    }


def format_skills_for_prompt(skills: list[dict[str, Any]], *, max_items: int = 40) -> str:
    """Compact bullet list for the LLM system prompt."""
    lines: list[str] = []
    for row in skills[:max_items]:
        name = row.get("name") or "?"
        desc = (row.get("description") or "").strip()
        if len(desc) > 120:
            desc = desc[:117] + "…"
        lines.append(f"- {name}: {desc}" if desc else f"- {name}")
    return "\n".join(lines) if lines else "(nenhuma skill encontrada em ~/.hermes/skills)"


def format_skills_for_user(skills: list[dict[str, Any]], recommended: list[str]) -> str:
    """Human-readable summary for the dashboard chat."""
    if not skills:
        return (
            "Nenhuma skill de desenvolvimento encontrada em ~/.hermes/skills. "
            "Rode `hermes skills list` no terminal."
        )
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for s in skills:
        cat = s.get("category") or "outros"
        by_cat.setdefault(cat, []).append(s)
    parts = ["Skills de desenvolvimento disponíveis no Hermes:\n"]
    for cat in sorted(by_cat):
        parts.append(f"\n**{cat}**")
        for row in by_cat[cat]:
            desc = (row.get("description") or "").strip()
            if desc:
                parts.append(f"- `{row['name']}` — {desc}")
            else:
                parts.append(f"- `{row['name']}`")
    if recommended:
        parts.append(
            "\nRecomendadas para agentes de código: "
            + ", ".join(f"`{s}`" for s in recommended)
        )
    return "\n".join(parts)


def normalize_skill_names(
    requested: list[str],
    known: set[str],
    *,
    defaults: list[str],
) -> list[str]:
    """Keep valid slugs; fall back to defaults when none match."""
    out: list[str] = []
    for raw in requested:
        slug = raw.strip().lower().replace(" ", "-")
        if slug in known and slug not in out:
            out.append(slug)
    if out:
        return out
    for d in defaults:
        if d in known and d not in out:
            out.append(d)
    return out
