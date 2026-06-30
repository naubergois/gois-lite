"""Slug normalization helpers shared by swarm robot modules."""

from __future__ import annotations

import unicodedata
from typing import Any

from .hermes_profiles import _normalize_name


def _norm_slug(value: str) -> str:
    return str(value or "").strip().lower()


def _strip_accents(value: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", value)
        if unicodedata.category(c) != "Mn"
    )


def _profile_slug(value: str) -> str:
    """Canonical Hermes profile slug (matches create_hermes_profile naming)."""
    text = str(value or "").strip()
    if not text:
        return ""
    return _normalize_name(text)


def _profile_slug_variants(value: str) -> set[str]:
    """Slug variants for matching assignee labels vs profile slugs."""
    text = str(value or "").strip()
    if not text:
        return set()
    variants = {_profile_slug(text), _profile_slug(_strip_accents(text))}
    return {v for v in variants if v}


def _build_assignee_alias_map(
    profiles_by_slug: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Map assignee tokens (slug or display name) to canonical profile slug."""
    out: dict[str, str] = {}
    for slug, meta in profiles_by_slug.items():
        canonical = _profile_slug(slug) or _norm_slug(slug)
        if not canonical:
            continue
        keys = {slug, canonical, meta.get("display_name", ""), meta.get("slug", "")}
        for key in keys:
            for variant in _profile_slug_variants(str(key or "")):
                out[variant] = canonical
    return out


def _resolve_assignee_slug(raw: str, alias_map: dict[str, str]) -> str:
    for variant in _profile_slug_variants(raw):
        if variant in alias_map:
            return alias_map[variant]
    variants = _profile_slug_variants(raw)
    return next(iter(variants), "")
