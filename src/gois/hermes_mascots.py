"""Procedural Hermes / swarm mascot catalog (200+ inline SVG avatars)."""

from __future__ import annotations

from typing import Callable, Optional

SvgBuilder = Callable[[str, str], str]


def _svg(body: str) -> str:
    return (
        '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg">'
        + body
        + "</svg>"
    )


def _eyes() -> str:
    return (
        '<circle cx="18" cy="22" r="2.5" fill="#0b1220"/>'
        '<circle cx="30" cy="22" r="2.5" fill="#0b1220"/>'
    )


def _smile() -> str:
    return (
        '<path d="M16 30 Q24 38 32 30" stroke="#0b1220" fill="none" stroke-width="2"/>'
    )


PALETTES: list[tuple[str, str, str]] = [
    ("Azul", "#3b82f6", "#1d4ed8"),
    ("Roxo", "#8b5cf6", "#6d28d9"),
    ("Laranja", "#f97316", "#c2410c"),
    ("Verde", "#22c55e", "#15803d"),
    ("Rosa", "#ec4899", "#be185d"),
    ("Ciano", "#06b6d4", "#0e7490"),
    ("Amarelo", "#eab308", "#a16207"),
    ("Vermelho", "#ef4444", "#b91c1c"),
    ("Teal", "#14b8a6", "#0f766e"),
    ("Índigo", "#6366f1", "#4338ca"),
]


def _round_face(primary: str, secondary: str) -> str:
    return _svg(
        f'<circle cx="24" cy="24" r="20" fill="{primary}"/>'
        + _eyes()
        + _smile()
        + f'<circle cx="24" cy="24" r="20" fill="none" stroke="{secondary}" stroke-width="1" opacity=".35"/>'
    )


def _pointy_ears(primary: str, secondary: str) -> str:
    return _svg(
        f'<polygon points="10,22 16,6 22,22" fill="{secondary}"/>'
        f'<polygon points="26,22 32,6 38,22" fill="{secondary}"/>'
        f'<circle cx="24" cy="28" r="14" fill="{primary}"/>'
        + _eyes()
        + _smile()
    )


def _owl_face(primary: str, secondary: str) -> str:
    return _svg(
        f'<ellipse cx="24" cy="26" rx="18" ry="16" fill="{primary}"/>'
        f'<circle cx="17" cy="22" r="5" fill="#f8fafc"/>'
        f'<circle cx="31" cy="22" r="5" fill="#f8fafc"/>'
        f'<circle cx="17" cy="22" r="2" fill="#0b1220"/>'
        f'<circle cx="31" cy="22" r="2" fill="#0b1220"/>'
        f'<polygon points="24,8 20,16 28,16" fill="{secondary}"/>'
    )


def _fox_face(primary: str, secondary: str) -> str:
    return _svg(
        f'<path d="M8 28 L24 8 L40 28 L32 40 L16 40 Z" fill="{primary}"/>'
        f'<path d="M8 28 L24 18 L40 28" fill="{secondary}" opacity=".45"/>'
        + _eyes()
        + f'<ellipse cx="24" cy="32" rx="4" ry="2" fill="#0b1220" opacity=".3"/>'
    )


def _robot_face(primary: str, secondary: str) -> str:
    return _svg(
        f'<rect x="10" y="14" width="28" height="26" rx="4" fill="{primary}"/>'
        f'<rect x="16" y="20" width="6" height="6" fill="{secondary}"/>'
        f'<rect x="26" y="20" width="6" height="6" fill="{secondary}"/>'
        f'<rect x="18" y="32" width="12" height="4" rx="1" fill="#334155"/>'
        f'<line x1="24" y1="8" x2="24" y2="14" stroke="{secondary}" stroke-width="2"/>'
        f'<circle cx="24" cy="7" r="3" fill="{secondary}"/>'
    )


def _penguin_face(primary: str, secondary: str) -> str:
    return _svg(
        f'<ellipse cx="24" cy="28" rx="12" ry="16" fill="{primary}"/>'
        f'<ellipse cx="24" cy="30" rx="7" ry="10" fill="#f8fafc"/>'
        f'<circle cx="24" cy="16" r="9" fill="{primary}"/>'
        f'<circle cx="20" cy="15" r="2" fill="#f8fafc"/>'
        f'<circle cx="28" cy="15" r="2" fill="#f8fafc"/>'
        f'<polygon points="24,18 20,24 28,24" fill="{secondary}"/>'
    )


def _fish_face(primary: str, secondary: str) -> str:
    return _svg(
        f'<ellipse cx="22" cy="24" rx="14" ry="10" fill="{primary}"/>'
        f'<polygon points="36,24 44,16 44,32" fill="{secondary}"/>'
        f'<circle cx="16" cy="22" r="2" fill="#0b1220"/>'
        f'<path d="M14 28 Q22 32 30 28" stroke="{secondary}" fill="none" stroke-width="2"/>'
    )


def _bug_face(primary: str, secondary: str) -> str:
    return _svg(
        f'<ellipse cx="24" cy="28" rx="12" ry="10" fill="{primary}"/>'
        f'<circle cx="24" cy="16" r="8" fill="{secondary}"/>'
        f'<line x1="16" y1="20" x2="8" y2="12" stroke="{secondary}" stroke-width="2"/>'
        f'<line x1="32" y1="20" x2="40" y2="12" stroke="{secondary}" stroke-width="2"/>'
        + _eyes()
    )


def _star_face(primary: str, secondary: str) -> str:
    return _svg(
        f'<polygon points="24,4 29,18 44,18 32,28 36,42 24,34 12,42 16,28 4,18 19,18" fill="{primary}"/>'
        f'<circle cx="20" cy="22" r="2" fill="#0b1220"/>'
        f'<circle cx="28" cy="22" r="2" fill="#0b1220"/>'
        f'<path d="M18 30 Q24 34 30 30" stroke="{secondary}" fill="none" stroke-width="2"/>'
    )


def _hex_face(primary: str, secondary: str) -> str:
    return _svg(
        f'<polygon points="24,6 38,14 38,34 24,42 10,34 10,14" fill="{primary}"/>'
        f'<polygon points="24,10 34,16 34,32 24,38 14,32 14,16" fill="{secondary}" opacity=".25"/>'
        + _eyes()
        + _smile()
    )


def _leaf_face(primary: str, secondary: str) -> str:
    return _svg(
        f'<path d="M24 6 C10 14 10 34 24 42 C38 34 38 14 24 6 Z" fill="{primary}"/>'
        f'<path d="M24 10 L24 38" stroke="{secondary}" stroke-width="2"/>'
        f'<path d="M24 20 Q16 18 12 24" stroke="{secondary}" fill="none" stroke-width="1.5"/>'
        f'<path d="M24 26 Q32 24 36 30" stroke="{secondary}" fill="none" stroke-width="1.5"/>'
    )


def _ghost_face(primary: str, secondary: str) -> str:
    return _svg(
        f'<path d="M12 14 H36 V32 C36 38 30 42 24 38 C18 42 12 38 12 32 Z" fill="{primary}"/>'
        f'<circle cx="18" cy="22" r="3" fill="#f8fafc"/>'
        f'<circle cx="30" cy="22" r="3" fill="#f8fafc"/>'
        f'<circle cx="18" cy="22" r="1.5" fill="#0b1220"/>'
        f'<circle cx="30" cy="22" r="1.5" fill="#0b1220"/>'
        f'<ellipse cx="24" cy="30" rx="3" ry="2" fill="{secondary}" opacity=".5"/>'
    )


def _dragon_face(primary: str, secondary: str) -> str:
    return _svg(
        f'<path d="M8 30 Q24 8 40 30 L34 40 H14 Z" fill="{primary}"/>'
        f'<polygon points="8,30 4,22 12,26" fill="{secondary}"/>'
        f'<polygon points="40,30 44,22 36,26" fill="{secondary}"/>'
        + _eyes()
        + f'<path d="M20 34 L24 38 L28 34" stroke="{secondary}" fill="none" stroke-width="2"/>'
    )


def _alien_face(primary: str, secondary: str) -> str:
    return _svg(
        f'<ellipse cx="24" cy="28" rx="14" ry="16" fill="{primary}"/>'
        f'<ellipse cx="17" cy="22" rx="4" ry="6" fill="{secondary}"/>'
        f'<ellipse cx="31" cy="22" rx="4" ry="6" fill="{secondary}"/>'
        f'<circle cx="17" cy="22" r="1.5" fill="#0b1220"/>'
        f'<circle cx="31" cy="22" r="1.5" fill="#0b1220"/>'
        f'<path d="M18 34 Q24 38 30 34" stroke="#0b1220" fill="none" stroke-width="1.5"/>'
    )


# Legacy mascots kept at the front for backward compatibility.
BASE_MASCOTS: list[dict[str, str]] = [
    {
        "id": "gois",
        "label": "Gois",
        "category": "clássicos",
        "svg": _round_face("#3b82f6", "#1d4ed8"),
    },
    {
        "id": "owl",
        "label": "Coruja",
        "category": "clássicos",
        "svg": _owl_face("#8b5cf6", "#a78bfa"),
    },
    {
        "id": "fox",
        "label": "Raposa",
        "category": "clássicos",
        "svg": _fox_face("#f97316", "#fb923c"),
    },
    {
        "id": "robot",
        "label": "Robô",
        "category": "clássicos",
        "svg": _robot_face("#64748b", "#22d3ee"),
    },
    {
        "id": "cat",
        "label": "Gato",
        "category": "clássicos",
        "svg": _pointy_ears("#eab308", "#ca8a04"),
    },
    {
        "id": "penguin",
        "label": "Pinguim",
        "category": "clássicos",
        "svg": _penguin_face("#1e293b", "#f97316"),
    },
]

# (slug, label, category, builder)
_SPECIES: list[tuple[str, str, str, SvgBuilder]] = [
    ("bear", "Urso", "animais", _round_face),
    ("lion", "Leão", "animais", _pointy_ears),
    ("tiger", "Tigre", "animais", _pointy_ears),
    ("wolf", "Lobo", "animais", _pointy_ears),
    ("dog", "Cachorro", "animais", _pointy_ears),
    ("bunny", "Coelho", "animais", _pointy_ears),
    ("deer", "Cervo", "animais", _pointy_ears),
    ("koala", "Coala", "animais", _round_face),
    ("panda", "Panda", "animais", _round_face),
    ("monkey", "Macaco", "animais", _round_face),
    ("elephant", "Elefante", "animais", _round_face),
    ("giraffe", "Girafa", "animais", _pointy_ears),
    ("zebra", "Zebra", "animais", _pointy_ears),
    ("hippo", "Hipopótamo", "animais", _round_face),
    ("rhino", "Rinoceronte", "animais", _round_face),
    ("sloth", "Preguiça", "animais", _round_face),
    ("kangaroo", "Canguru", "animais", _pointy_ears),
    ("platypus", "Ornitorrinco", "animais", _round_face),
    ("dolphin", "Golfinho", "animais", _fish_face),
    ("whale", "Baleia", "animais", _fish_face),
    ("shark", "Tubarão", "animais", _fish_face),
    ("octopus", "Polvo", "animais", _round_face),
    ("crab", "Caranguejo", "animais", _bug_face),
    ("turtle", "Tartaruga", "animais", _round_face),
    ("frog", "Sapo", "animais", _round_face),
    ("snake", "Cobra", "animais", _fish_face),
    ("lizard", "Lagarto", "animais", _pointy_ears),
    ("eagle", "Águia", "animais", _pointy_ears),
    ("hawk", "Falcão", "animais", _pointy_ears),
    ("parrot", "Papagaio", "animais", _pointy_ears),
    ("flamingo", "Flamingo", "animais", _pointy_ears),
    ("duck", "Pato", "animais", _round_face),
    ("chicken", "Galinha", "animais", _pointy_ears),
    ("bee", "Abelha", "animais", _bug_face),
    ("butterfly", "Borboleta", "animais", _bug_face),
    ("spider", "Aranha", "animais", _bug_face),
    ("snail", "Caracol", "animais", _round_face),
    ("hedgehog", "Ouriço", "animais", _round_face),
    ("squirrel", "Esquilo", "animais", _pointy_ears),
    ("bot", "Bot", "robôs", _robot_face),
    ("droid", "Droid", "robôs", _robot_face),
    ("mech", "Mech", "robôs", _hex_face),
    ("cyborg", "Ciborgue", "robôs", _robot_face),
    ("android", "Android", "robôs", _robot_face),
    ("chip", "Chip", "robôs", _hex_face),
    ("cpu", "CPU", "robôs", _hex_face),
    ("server", "Servidor", "robôs", _hex_face),
    ("drone", "Drone", "robôs", _hex_face),
    ("rocket", "Foguete", "robôs", _star_face),
    ("satellite", "Satélite", "robôs", _hex_face),
    ("dragon", "Dragão", "fantasia", _dragon_face),
    ("phoenix", "Fênix", "fantasia", _star_face),
    ("unicorn", "Unicórnio", "fantasia", _pointy_ears),
    ("ghost", "Fantasma", "fantasia", _ghost_face),
    ("alien", "Alienígena", "fantasia", _alien_face),
    ("wizard", "Mago", "fantasia", _star_face),
    ("knight", "Cavaleiro", "fantasia", _hex_face),
    ("fairy", "Fada", "fantasia", _star_face),
    ("goblin", "Goblin", "fantasia", _pointy_ears),
    ("yeti", "Yeti", "fantasia", _round_face),
    ("mermaid", "Sereia", "fantasia", _fish_face),
    ("leaf", "Folha", "natureza", _leaf_face),
    ("tree", "Árvore", "natureza", _leaf_face),
    ("flower", "Flor", "natureza", _star_face),
    ("mushroom", "Cogumelo", "natureza", _round_face),
    ("cactus", "Cacto", "natureza", _leaf_face),
    ("sun", "Sol", "natureza", _star_face),
    ("moon", "Lua", "natureza", _round_face),
    ("cloud", "Nuvem", "natureza", _round_face),
    ("rain", "Chuva", "natureza", _star_face),
    ("snow", "Neve", "natureza", _star_face),
    ("gem", "Gema", "natureza", _hex_face),
    ("crystal", "Cristal", "natureza", _hex_face),
]


def _build_catalog() -> list[dict[str, str]]:
    catalog: list[dict[str, str]] = [dict(m) for m in BASE_MASCOTS]
    seen = {m["id"] for m in catalog}

    for slug, label, category, builder in _SPECIES:
        for idx, (tone, primary, secondary) in enumerate(PALETTES, start=1):
            mid = f"{slug}-{idx}"
            if mid in seen:
                continue
            seen.add(mid)
            catalog.append(
                {
                    "id": mid,
                    "label": f"{label} {tone}",
                    "category": category,
                    "svg": builder(primary, secondary),
                }
            )
    return catalog


HERMES_MASCOTS: list[dict[str, str]] = _build_catalog()
_MASCOT_BY_ID: dict[str, dict[str, str]] = {m["id"]: m for m in HERMES_MASCOTS}
_VALID_MASCOT_IDS: set[str] = set(_MASCOT_BY_ID)


def list_mascots() -> list[dict[str, str]]:
    return [
        {"id": m["id"], "label": m["label"], "category": m.get("category", ""), "svg": m["svg"]}
        for m in HERMES_MASCOTS
    ]


def normalize_mascot(raw: Optional[str], *, default: str = "gois") -> str:
    mid = (raw or default).strip().lower()
    return mid if mid in _VALID_MASCOT_IDS else default


def mascot_label(mascot_id: str) -> str:
    row = _MASCOT_BY_ID.get(mascot_id.strip().lower())
    return row["label"] if row else mascot_id


def mascot_svg(mascot_id: str) -> str:
    row = _MASCOT_BY_ID.get(mascot_id.strip().lower())
    if row:
        return row["svg"]
    fallback = _MASCOT_BY_ID.get("robot") or _MASCOT_BY_ID.get("gois")
    return fallback["svg"] if fallback else ""
