#!/usr/bin/env python3
"""Copy/symlink gois-lite skills: kanban only."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# Kanban workflow helpers (slug contains one of these needles).
_KANBAN_NEEDLES = (
    "kanban",
    "card-release",
    "priority-queue",
    "ide-handoff",
)


def _is_lite_skill(skill_dir: Path) -> bool:
    slug = skill_dir.name.lower()
    return any(needle in slug for needle in _KANBAN_NEEDLES)


def filter_skills(*, source: Path, target: Path, use_symlinks: bool = True) -> int:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    count = 0
    for entry in sorted(source.iterdir()):
        if not entry.is_dir():
            continue
        if not _is_lite_skill(entry):
            continue
        dest = target / entry.name
        if use_symlinks:
            dest.symlink_to(entry.resolve())
        else:
            shutil.copytree(entry, dest, symlinks=True)
        count += 1

    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-skills", type=Path, required=True)
    parser.add_argument("--target-skills", type=Path, required=True)
    parser.add_argument(
        "--copy",
        action="store_true",
        help="copy trees instead of symlinks (default: symlinks)",
    )
    args = parser.parse_args()

    if not args.source_skills.is_dir():
        print(f"source missing: {args.source_skills}", file=sys.stderr)
        return 1

    n = filter_skills(
        source=args.source_skills,
        target=args.target_skills,
        use_symlinks=not args.copy and os.environ.get("GOIS_LITE_COPY_SKILLS") != "1",
    )
    print(f"filtered {n} kanban skills → {args.target_skills}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
