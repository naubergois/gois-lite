#!/usr/bin/env python3
"""Remove gois-lite copy bloat — keep Chat + Kanban + MCP cards only."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Alternate MCP servers and CLI entrypoints not used in lite.
_TOP_LEVEL_DROP = (
    "api_credits.py",
    "auto_skill_creator.py",
    "backup_manager.py",
    "backups_api.py",
    "chat_sse_stream.py",
    "desktop_control.py",
    "email_editor_signatures.py",
    "email_signatures.py",
    "email_signatures_tools.py",
    "email_trabalhos_ops.py",
    "google_calendar.py",
    "hotmart_mcp.py",
    "kanban_execution_history.py",
    "latex_editor_backup_panel.py",
    "mcp_book_generator_server.py",
    "mcp_http_server.py",
    "mcp_jobs_server.py",
    "mcp_memclaw_server.py",
    "mcp_overleaf_templates_server.py",
    "mcp_overleaf_wrapper.py",
    "mcp_skills_server.py",
    "mcp_sse_server.py",
    "mcp_swarm_server.py",
    "memory_file_index.py",
    "notebook_trabalhos_ops.py",
    "overleaf_projects_config.py",
    "project_agent_group_notifier.py",
    "status_app.py",
    "swarm_course_skills_migrate.py",
    "swarm_execute_ops.py",
    "swarm_execution_history.py",
    "trabalhos_alunos_store.py",
    "wellhub_api.py",
)

# Dashboard pages not loaded in gois-lite (dashboard.py skips them).
_LITE_PAGE_DROP = (
    "monitor_page.py",
    "ruflo_chat_page.py",
    "perguntas_page.py",
    "errors_page.py",
    "health_page.py",
    "skills_page.py",
    "active_agents_page.py",
    "cron_slots_page.py",
    "cron_costs_page.py",
    "roles_page.py",
    "users_page.py",
    "teams_page.py",
    "team_create_page.py",
    "metrics_page.py",
    "latex_page.py",
    "allowlist_page.py",
    "env_keys_page.py",
    "agenda_page.py",
    "swarm_page.py",
    "mcp_servers_page.py",
    "agent_create_page.py",
    "cron_create_page.py",
    "ide_page.py",
    "entity_db_page.py",
    "knowledge_page.py",
    "project_memory_page.py",
    "projects_page.py",
    "calendar_page.py",
    "latex_dashboard.py",
    "manage_delete_page.py",
    "model_usage_page.py",
    "team_detail_page.py",
    "team_messages_page.py",
    "ruflo_engines_page.py",
    "article_quality_page.py",
    "team_file_preview_popup_page.py",
    "priority_queue_page.py",
    "status_panel_page.py",
)

_REPO_DIR_DROP = (
    ".git",
    ".github",
    ".swarm",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "agents",
    ".agents",
    "data",
    "curso-consultoria-1",
    ".claude",
    ".codex",
    ".claude-flow",
    "vendor",
    "skills",
    "hermes",
    "tests",
    "teams",
    "var",
    "output",
    "models",
)

_REPO_FILE_DROP = (
    "README.pt.md",
    "README.zh-CN.md",
    "README-WINDOWS.md",
    "AGENTS.md",
    "CLAUDE.md",
    "CODEX.md",
)

# Only these top-level entries survive in gois-lite after prune.
_LITE_ROOT_DIR_KEEP = frozenset(
    {
        "src",
        "scripts",
        "hermes",
        ".cursor",
        ".kiro",
        ".stack",
        ".venv",
    }
)

# Removed after runtime prune even if import closure pulled them in.
_LITE_FORCE_DROP_FILES = (
    "openclaw_chat_whatsapp_groups.py",
    "monitor_whatsapp_inbound.py",
    "monitor_whatsapp_outbound.py",
    "wacli_auth.py",
    "wacli_busy.py",
    "wacli_runner.py",
    "wacli_sync.py",
    "whatsapp_allowlist.py",
    "whatsapp_allowlist_mongo.py",
    "whatsapp_allowlist_store.py",
    "whatsapp_digest.py",
    "whatsapp_inbound.py",
    "whatsapp_outbound.py",
)

_LITE_ROOT_FILE_KEEP = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "config.yaml",
        "config.example.yaml",
        "README.md",
        ".gois-lite",
        ".mcp.json",
        ".gitignore",
        ".env",
        ".env.example",
    }
)

_SCRIPTS_KEEP = frozenset(
    {
        "start.sh",
        "start.ps1",
        "setup_mongo.sh",
        "lib",
    }
)

_KDP_KEEP = frozenset({"__init__.py", "manuscript.py"})

_RV_KEEP = (
    ("__init__.py", "__init__.py"),
    ("agents/metadata_agent.py", "agents/metadata_agent.py"),
)

# Lazy-import targets kept even when not in static closure.
_MODULE_KEEP_EXTRA = frozenset(
    {
        "gois.roteiro_viral",
        "gois.roteiro_viral.agents",
        "gois.roteiro_viral.agents.metadata_agent",
        "gois.kanban_ide_handoff",
        "gois.kanban_ide_handoff_dispatch",
        "gois.kanban_ide_handoff_ops",
        "gois.kanban_project_zip",
        "gois.mcp_cards_page",
        "gois.gois_lite_ui",
    }
)

_ASSET_RE = re.compile(r"""load_asset\s*\(\s*['"]([^'"]+)['"]""")
_THEME_ASSETS = frozenset(
    {
        "app_theme.css",
        "app_ui.css",
        "app_nav.css",
        "dark_mode.js",
        "gois_mascot.svg",
    }
)


def _rm(path: Path, *, dry_run: bool) -> bool:
    if not path.exists():
        return False
    if dry_run:
        print(f"would remove {path}")
        return True
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return True


def _path_to_module(py: Path, *, gois_dir: Path) -> str:
    rel = py.relative_to(gois_dir)
    if rel.name == "__init__.py":
        parts = rel.parts[:-1]
    else:
        parts = rel.with_suffix("").parts
    return "gois." + ".".join(parts)


def _compute_runtime_modules(*, target: Path, python_bin: str) -> set[str]:
    src = target / "src"
    env = os.environ.copy()
    env["GOIS_LITE"] = "1"
    env["MONGODB_DB"] = "gois_lite"
    env["PYTHONPATH"] = str(src)
    code = """
import sys
for key in list(sys.modules):
    if key.startswith("gois."):
        del sys.modules[key]
import gois.__main__  # noqa: F401
import gois.mcp_cards_server  # noqa: F401
import gois.kanban_ide_handoff_dispatch  # noqa: F401
import gois.webapp  # noqa: F401
print("\\n".join(sorted(k for k in sys.modules if k.startswith("gois."))))
"""
    proc = subprocess.run(
        [python_bin, "-c", code],
        cwd=str(target),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"runtime import probe failed:\n{proc.stderr or proc.stdout}"
        )
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _copy_rv_stub(*, source: Path, target: Path, dry_run: bool) -> None:
    rv_src = source / "src" / "gois" / "roteiro_viral"
    rv_dst = target / "src" / "gois" / "roteiro_viral"
    if dry_run:
        print(f"would replace {rv_dst} with metadata stub")
        return
    if rv_dst.exists():
        shutil.rmtree(rv_dst)
    rv_dst.mkdir(parents=True)
    (rv_dst / "agents").mkdir(parents=True)
    for rel_src, rel_dst in _RV_KEEP:
        shutil.copy2(rv_src / rel_src, rv_dst / rel_dst)


def _collect_asset_refs(*, gois_dir: Path, runtime_mods: set[str]) -> set[str]:
    kept: set[str] = set(_THEME_ASSETS)
    for py in gois_dir.rglob("*.py"):
        mod = _path_to_module(py, gois_dir=gois_dir)
        if mod not in runtime_mods and mod not in _MODULE_KEEP_EXTRA:
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        kept.update(_ASSET_RE.findall(text))
    return kept


def clean_lite_root(*, target: Path, dry_run: bool = False) -> int:
    """Drop stray top-level files/dirs not needed for gois-lite."""
    removed = 0
    for child in list(target.iterdir()):
        name = child.name
        if child.is_dir() and name in _LITE_ROOT_DIR_KEEP:
            continue
        if child.is_file() and name in _LITE_ROOT_FILE_KEEP:
            continue
        if dry_run:
            print(f"would remove root {child}")
        else:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        removed += 1
    return removed


def prune_gois_lite(
    *,
    source: Path,
    target: Path,
    python_bin: str,
    dry_run: bool = False,
) -> dict[str, int]:
    stats = {"dirs": 0, "files": 0, "bytes": 0}

    def count_removed(path: Path) -> None:
        if path.is_dir():
            stats["dirs"] += 1
            for child in path.rglob("*"):
                if child.is_file():
                    stats["files"] += 1
                    try:
                        stats["bytes"] += child.stat().st_size
                    except OSError:
                        pass
        elif path.is_file():
            stats["files"] += 1
            try:
                stats["bytes"] += path.stat().st_size
            except OSError:
                pass

    for name in _REPO_DIR_DROP:
        p = target / name
        if p.exists():
            count_removed(p)
            _rm(p, dry_run=dry_run)

    for name in _REPO_FILE_DROP:
        p = target / name
        if p.exists():
            count_removed(p)
            _rm(p, dry_run=dry_run)

    for pattern in ("*.log", "*.log.*", "*.db", "*.jpeg", "*.jpg", "*.png", "*.bak", ".DS_Store"):
        for p in target.glob(pattern):
            if p.is_file():
                count_removed(p)
                _rm(p, dry_run=dry_run)

    gois_dir = target / "src" / "gois"

    for name in _TOP_LEVEL_DROP + _LITE_PAGE_DROP:
        p = gois_dir / name
        if p.exists():
            count_removed(p)
            _rm(p, dry_run=dry_run)

    hotmart = gois_dir / "hotmart"
    if hotmart.exists():
        count_removed(hotmart)
        _rm(hotmart, dry_run=dry_run)

    scripts_pkg = gois_dir / "scripts"
    if scripts_pkg.exists():
        count_removed(scripts_pkg)
        _rm(scripts_pkg, dry_run=dry_run)

    kdp = gois_dir / "kdp"
    if kdp.is_dir():
        for child in list(kdp.iterdir()):
            if child.name not in _KDP_KEEP:
                count_removed(child)
                _rm(child, dry_run=dry_run)
        init_py = kdp / "__init__.py"
        stub = (
            '"""KDP stub for gois-lite."""\n'
            "from .manuscript import ManuscriptBuilder\n\n"
            '__all__ = ["ManuscriptBuilder"]\n'
        )
        if not dry_run:
            init_py.write_text(stub, encoding="utf-8")

    rv = gois_dir / "roteiro_viral"
    if rv.exists():
        count_removed(rv)
    _copy_rv_stub(source=source, target=target, dry_run=dry_run)

    scripts_dir = target / "scripts"
    if scripts_dir.is_dir():
        for child in list(scripts_dir.iterdir()):
            if child.name in _SCRIPTS_KEEP:
                continue
            count_removed(child)
            _rm(child, dry_run=dry_run)

    runtime_mods: set[str] = set()
    if not dry_run:
        runtime_mods = _compute_runtime_modules(target=target, python_bin=python_bin)
        runtime_mods |= _MODULE_KEEP_EXTRA

        for py in list(gois_dir.rglob("*.py")):
            mod = _path_to_module(py, gois_dir=gois_dir)
            if mod in runtime_mods:
                continue
            count_removed(py)
            _rm(py, dry_run=False)

        # Drop empty package dirs (bottom-up).
        for d in sorted(gois_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                count_removed(d)
                _rm(d, dry_run=False)

        asset_refs = _collect_asset_refs(gois_dir=gois_dir, runtime_mods=runtime_mods)
        assets = gois_dir / "assets"
        if assets.is_dir():
            for asset in list(assets.rglob("*")):
                if not asset.is_file():
                    continue
                rel = asset.relative_to(assets).as_posix()
                if rel in asset_refs or rel.startswith("components/"):
                    continue
                count_removed(asset)
                _rm(asset, dry_run=False)

        root_removed = clean_lite_root(target=target, dry_run=False)
        if root_removed:
            print(f"removed {root_removed} stray root entries under {target}")

        gois_dir = target / "src" / "gois"
        for name in _LITE_FORCE_DROP_FILES:
            p = gois_dir / name
            if p.is_file():
                count_removed(p)
                _rm(p, dry_run=False)

        qmon = target / "src" / "qclawmonitor"
        if qmon.exists():
            count_removed(qmon)
            _rm(qmon, dry_run=False)

        kiro = target / ".kiro"
        if kiro.is_dir():
            for child in list(kiro.iterdir()):
                if child.name == "settings" and child.is_dir():
                    for sub in list(child.iterdir()):
                        if sub.name == "mcp.json":
                            continue
                        count_removed(sub)
                        _rm(sub, dry_run=False)
                    continue
                count_removed(child)
                _rm(child, dry_run=False)

    return stats


def verify_imports(*, target: Path, python_bin: str) -> None:
    env = os.environ.copy()
    env["GOIS_LITE"] = "1"
    env["MONGODB_DB"] = "gois_lite"
    env["PYTHONPATH"] = str(target / "src")
    code = """
import gois.__main__  # noqa: F401
import gois.mcp_cards_server  # noqa: F401
import gois.kanban_ide_handoff_dispatch  # noqa: F401
import gois.webapp  # noqa: F401
import gois.metrics_http  # noqa: F401
"""
    proc = subprocess.run(
        [python_bin, "-c", code],
        cwd=str(target),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Main gois repo")
    parser.add_argument("--target", type=Path, required=True, help="gois-lite directory")
    parser.add_argument("--python-bin", type=str, default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-verify", action="store_true")
    args = parser.parse_args()

    python_bin = args.python_bin.strip()
    if not python_bin:
        for candidate in (
            args.target / ".venv/bin/python",
            args.source / ".venv/bin/python",
        ):
            if candidate.is_file():
                python_bin = str(candidate)
                break
    if not python_bin:
        python_bin = sys.executable

    stats = prune_gois_lite(
        source=args.source,
        target=args.target,
        python_bin=python_bin,
        dry_run=args.dry_run,
    )
    mb = stats["bytes"] / (1024 * 1024)
    print(
        f"pruned ~{stats['files']} files in {stats['dirs']} dirs "
        f"({mb:.1f} MiB) under {args.target}"
    )

    if args.dry_run or args.skip_verify:
        return 0

    verify_imports(target=args.target, python_bin=python_bin)
    print("import check ok (__main__, mcp_cards, webapp, metrics_http)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
