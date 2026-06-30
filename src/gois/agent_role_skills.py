"""Role preset inference and default skills for Hermes / swarm agents."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .hermes_profile_model import write_profile_meta
from .hermes_profiles import hermes_profiles_root, preset_default_skills

MIN_SWARM_PROFILE_SKILLS = 4

# Substrings in agent profile/name → canonical preset id (most specific first).
_NAME_PRESET_HINTS: tuple[tuple[str, str], ...] = (
    ("frontend-dev", "frontend-dev"),
    ("backend-dev", "backend-dev"),
    ("fullstack-dev", "fullstack-dev"),
    ("tech-lead", "tech-lead"),
    ("qa-engineer", "qa-engineer"),
    ("infra-dev", "devops"),
    ("devops", "devops"),
    ("tester", "qa-engineer"),
    ("mobile-dev", "mobile-dev"),
    ("data-engineer", "data-engineer"),
    ("security", "security"),
    ("ux-designer", "ux-designer"),
    ("product-manager", "product-manager"),
    ("ai-engineer", "ai-engineer"),
)

ROLE_FIX_SKILLS: dict[str, list[str]] = {
    "tech-lead": [
        "kanban-summary",
        "qclaw-chat-swarm-manage",
        "requesting-code-review",
        "writing-plans",
    ],
    "backend-dev": [
        "requesting-code-review",
        "test-driven-development",
        "codebase-improvement-analysis",
        "qclaw-chat-errors",
    ],
    "frontend-dev": [
        "requesting-code-review",
        "test-driven-development",
        "qclaw-chat-slides-pdf",
        "codebase-improvement-analysis",
    ],
    "devops": [
        "qclaw-chat-monitor-update",
        "qclaw-chat-aws-manage",
        "qclaw-chat-jobs-health",
        "systematic-debugging",
    ],
    "qa-engineer": [
        "test-driven-development",
        "systematic-debugging",
        "requesting-code-review",
        "qclaw-chat-errors",
    ],
}


def infer_role_preset_id(
    *,
    name: str = "",
    role: str = "",
    instructions: str = "",
    explicit: str = "",
) -> str:
    """Infer canonical role preset from explicit id or agent name/role."""
    explicit_id = str(explicit or "").strip()
    if explicit_id:
        return explicit_id
    for blob in (name, role):
        low = str(blob or "").lower()
        if not low:
            continue
        for needle, preset in _NAME_PRESET_HINTS:
            if needle in low:
                return preset
    return ""


def role_skill_slugs(preset_id: str) -> list[str]:
    """Preset-specific skills merged with optional catalogue defaults."""
    pid = str(preset_id or "").strip()
    if not pid:
        return []
    merged: list[str] = []
    seen: set[str] = set()
    for slug in list(ROLE_FIX_SKILLS.get(pid, ())) + list(preset_default_skills(pid)):
        key = slug.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(slug)
    return merged


def swarm_profile_meta_extras(
    *,
    workdir: str | None,
    skills: list[str],
) -> dict[str, Any]:
    """Keys that must land in profile.yaml at swarm creation time."""
    meta: dict[str, Any] = {}
    wd = str(workdir or "").strip()
    if wd:
        meta["workdir"] = wd
    if skills:
        meta["skills"] = list(skills)
    return meta


def _read_profile_meta(profile_name: str) -> dict[str, Any]:
    path = hermes_profiles_root().expanduser() / profile_name / "profile.yaml"
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def persist_swarm_profile_fields(
    profile_name: str,
    *,
    workdir: str | None = None,
    skills: list[str] | None = None,
    role_preset: str | None = None,
    role_preset_label: str | None = None,
    swarm_name: str | None = None,
) -> dict[str, Any]:
    """Idempotently ensure profile.yaml has integration fields after swarm create."""
    slug = str(profile_name or "").strip()
    if not slug:
        return {"ok": False, "error": "profile_name is required"}

    updates: dict[str, Any] = {}
    meta = _read_profile_meta(slug)

    wd = str(workdir or "").strip()
    if wd and not str(meta.get("workdir") or "").strip():
        updates["workdir"] = wd

    if skills:
        existing = meta.get("skills")
        existing_list = (
            [str(s).strip() for s in existing if str(s).strip()]
            if isinstance(existing, list)
            else []
        )
        if len(existing_list) < MIN_SWARM_PROFILE_SKILLS:
            updates["skills"] = list(skills)

    preset = str(role_preset or "").strip()
    if preset:
        target = infer_role_preset_id(name=slug, explicit=preset) or preset
        current = str(meta.get("role_preset") or "").strip()
        if target != current:
            updates["role_preset"] = target
            label = str(role_preset_label or "").strip()
            if label:
                updates["role_preset_label"] = label

    swarm = str(swarm_name or "").strip()
    if swarm and not str(meta.get("swarm_name") or "").strip():
        updates["swarm_name"] = swarm

    if updates:
        if not write_profile_meta(slug, updates):
            return {"ok": False, "error": f"falha ao gravar profile.yaml de {slug}"}
        verify_after = verify_swarm_profile_on_disk(
            slug,
            workdir=wd or None,
            min_skills=1 if skills else MIN_SWARM_PROFILE_SKILLS,
        )
        missing = [i for i in (verify_after.get("issues") or []) if "profile.yaml ausente" in i]
        if missing:
            return {"ok": False, "error": "; ".join(missing)}

    return {"ok": True, "profile": slug, "patched": sorted(updates.keys())}


def verify_swarm_profile_on_disk(
    profile_name: str,
    *,
    workdir: str | None = None,
    min_skills: int = MIN_SWARM_PROFILE_SKILLS,
) -> dict[str, Any]:
    """Confirm profile.yaml on disk satisfies swarm integration requirements."""
    slug = str(profile_name or "").strip()
    issues: list[str] = []
    meta = _read_profile_meta(slug)
    if not meta:
        return {"ok": False, "issues": ["profile.yaml ausente ou ilegível"]}

    disk_wd = str(meta.get("workdir") or "").strip()
    expected_wd = str(workdir or "").strip()
    if expected_wd and not disk_wd:
        issues.append("workdir não persistido em profile.yaml")
    elif not disk_wd and not expected_wd:
        issues.append("workdir ausente em profile.yaml")

    raw_skills = meta.get("skills")
    skill_count = (
        len([s for s in raw_skills if str(s).strip()])
        if isinstance(raw_skills, list)
        else 0
    )
    if skill_count < min_skills:
        issues.append(f"skills insuficientes no profile.yaml ({skill_count}<{min_skills})")

    if not str(meta.get("role_preset") or "").strip():
        issues.append("role_preset ausente em profile.yaml")

    return {"ok": not issues, "issues": issues, "profile": slug}
