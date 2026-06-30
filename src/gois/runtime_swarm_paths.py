"""Canonical on-disk layout and store keys for swarm runtime blobs.

Production code stores swarm definitions at ``.stack/swarms/{slug}.json`` and
graph checkpoints at ``.stack/swarms/{slug}/graph_state.json`` (see
``openai_swarm`` and ``swarm_graph``). Migration helpers in ``runtime_state``
and ``runtime_blobs_mongo`` must use the same layout.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Iterator

SWARM_STATE_DIRNAME = "swarms"
SWARM_CHECKPOINT_FILENAME = "graph_state.json"
SWARM_EVAL_HISTORY_FILENAME = "eval_history.jsonl"
SWARM_BLACKBOARD_FILENAME = "blackboard.jsonl"
SWARM_CANCEL_FLAG_FILENAME = "graph_cancel.flag"


def swarm_def_key(slug: str) -> str:
    return f"swarm:def:{slug}:state"


def swarm_checkpoint_key(slug: str) -> str:
    return f"swarm:{slug}:checkpoint"


def swarm_eval_history_key(slug: str) -> str:
    return f"swarm:{slug}:eval_history"


def swarm_cancel_key(slug: str) -> str:
    return f"swarm:{slug}:cancel"


def swarm_blackboard_key(slug: str) -> str:
    return f"swarm:{slug}:blackboard"


def swarm_definitions_dir(stack_root: Path) -> Path:
    return stack_root.expanduser().resolve() / SWARM_STATE_DIRNAME


def swarm_definition_path(stack_root: Path, slug: str) -> Path:
    return swarm_definitions_dir(stack_root) / f"{slug}.json"


def swarm_checkpoint_path(stack_root: Path, slug: str) -> Path:
    return swarm_definitions_dir(stack_root) / slug / SWARM_CHECKPOINT_FILENAME


def swarm_eval_history_path(stack_root: Path, slug: str) -> Path:
    return swarm_definitions_dir(stack_root) / slug / SWARM_EVAL_HISTORY_FILENAME


def swarm_cancel_flag_path(stack_root: Path, slug: str) -> Path:
    return swarm_definitions_dir(stack_root) / slug / SWARM_CANCEL_FLAG_FILENAME


def swarm_blackboard_path(stack_root: Path, slug: str) -> Path:
    return swarm_definitions_dir(stack_root) / slug / SWARM_BLACKBOARD_FILENAME


def iter_swarm_definition_sources(
    stack_root: Path,
) -> Iterator[tuple[str, Path]]:
    """Yield ``(store_key, legacy_path)`` for each swarm definition JSON."""
    definitions_dir = swarm_definitions_dir(stack_root)
    if not definitions_dir.is_dir():
        return
    for state_file in sorted(definitions_dir.glob("*.json")):
        yield swarm_def_key(state_file.stem), state_file


def iter_swarm_checkpoint_sources(
    stack_root: Path,
) -> Iterator[tuple[str, Path]]:
    """Yield ``(store_key, legacy_path)`` for each swarm graph checkpoint."""
    definitions_dir = swarm_definitions_dir(stack_root)
    if not definitions_dir.is_dir():
        return
    for swarm_subdir in sorted(definitions_dir.iterdir()):
        if not swarm_subdir.is_dir():
            continue
        checkpoint = swarm_subdir / SWARM_CHECKPOINT_FILENAME
        if checkpoint.is_file():
            yield swarm_checkpoint_key(swarm_subdir.name), checkpoint


def iter_swarm_eval_history_sources(
    stack_root: Path,
) -> Iterator[tuple[str, Path]]:
    """Yield ``(store_key, legacy_path)`` for each swarm eval history JSONL."""
    definitions_dir = swarm_definitions_dir(stack_root)
    if not definitions_dir.is_dir():
        return
    for swarm_subdir in sorted(definitions_dir.iterdir()):
        if not swarm_subdir.is_dir():
            continue
        history = swarm_subdir / SWARM_EVAL_HISTORY_FILENAME
        if history.is_file():
            yield swarm_eval_history_key(swarm_subdir.name), history


def iter_swarm_blackboard_sources(
    stack_root: Path,
) -> Iterator[tuple[str, Path]]:
    """Yield ``(store_key, legacy_path)`` for each swarm blackboard JSONL."""
    definitions_dir = swarm_definitions_dir(stack_root)
    if not definitions_dir.is_dir():
        return
    for swarm_subdir in sorted(definitions_dir.iterdir()):
        if not swarm_subdir.is_dir():
            continue
        board = swarm_subdir / SWARM_BLACKBOARD_FILENAME
        if board.is_file():
            yield swarm_blackboard_key(swarm_subdir.name), board


def migrate_swarm_files(
    stack_root: Path,
    migrate_fn: Callable[[str, Path], bool],
) -> dict[str, int]:
    """Import swarm definition/checkpoint files via ``migrate_fn(key, path)``."""
    counts = {"swarm_checkpoints": 0, "swarm_definitions": 0}
    for key, path in iter_swarm_definition_sources(stack_root):
        if migrate_fn(key, path):
            counts["swarm_definitions"] += 1
    for key, path in iter_swarm_checkpoint_sources(stack_root):
        if migrate_fn(key, path):
            counts["swarm_checkpoints"] += 1
    return counts
