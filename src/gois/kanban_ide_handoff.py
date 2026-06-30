"""Prepare kanban card context for IDE-assisted development (Kiro, Cursor, etc.)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

IDE_EXECUTION_BACKENDS = frozenset(
    {"kiro", "cursor", "vscode", "copilot", "antigravity"}
)

EXECUTION_BACKEND_LABELS: dict[str, str] = {
    "llm": "LLM (Hermes)",
    "kiro": "Kiro",
    "cursor": "Cursor",
    "vscode": "VS Code",
    "copilot": "Copilot (VS Code)",
    "antigravity": "Antigravity",
}

_IDE_CLI: dict[str, tuple[str, ...]] = {
    "kiro": ("kiro",),
    "cursor": ("cursor",),
    "vscode": ("code",),
    "copilot": ("code",),
    "antigravity": ("antigravity",),
}


def normalize_execution_backend(value: Any) -> str:
    """Return a canonical execution backend id (defaults to ``llm``)."""
    raw = str(value or "").strip().lower()
    if raw in EXECUTION_BACKEND_LABELS:
        return raw
    aliases = {
        "hermes": "llm",
        "model": "llm",
        "llm": "llm",
        "vs code": "vscode",
        "vscode": "vscode",
        "visual studio code": "vscode",
        "github copilot": "copilot",
        "copilot": "copilot",
    }
    return aliases.get(raw, "llm")


def execution_backend_label(backend: str) -> str:
    key = normalize_execution_backend(backend)
    return EXECUTION_BACKEND_LABELS.get(key, key)


def is_ide_execution_backend(backend: str) -> bool:
    return normalize_execution_backend(backend) in IDE_EXECUTION_BACKENDS


def list_execution_backends() -> list[dict[str, str]]:
    return [
        {"id": key, "label": label}
        for key, label in EXECUTION_BACKEND_LABELS.items()
        if key in IDE_EXECUTION_BACKENDS
    ]


def suggest_ide_for_task(task: dict[str, Any]) -> str:
    """Heuristic IDE choice from card skills/description."""
    skills_raw = task.get("skills") or []
    if isinstance(skills_raw, str):
        skills = [s.strip().lower() for s in re.split(r"[\n,;]", skills_raw) if s.strip()]
    else:
        skills = [str(s).strip().lower() for s in skills_raw if str(s).strip()]
    joined = " ".join(skills)
    if "kiro" in joined or "qclaw-kiro-dev" in joined:
        return "kiro"
    if "cursor" in joined:
        return "cursor"
    if "antigravity" in joined:
        return "antigravity"
    if "copilot" in joined:
        return "copilot"
    if "vscode" in joined or "vs code" in joined:
        return "vscode"

    desc = str(
        task.get("description") or task.get("text") or task.get("body") or ""
    ).lower()
    if any(
        token in desc
        for token in (
            "browser",
            "navegador",
            "api externa",
            "validar ui",
            "validação visual",
            "screenshot",
        )
    ):
        return "antigravity"
    if any(token in desc for token in ("spec", "requirements", "design doc", "ears")):
        return "kiro"
    return "cursor"


def _slug_from_task(task_id: str, title: str) -> str:
    text = f"{task_id}-{title}".lower()
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (slug[:64] or task_id).strip("-") or task_id


def _common_markdown(
    *,
    task_id: str,
    title: str,
    description: str,
    column: str,
    priority: Any,
    assignees: list[str],
    skills: list[str],
    workdir: str,
    kanban_file: str,
    acceptance: str,
    ide: str,
    base_url: str,
) -> str:
    assignee_text = ", ".join(assignees) if assignees else "—"
    skills_text = ", ".join(skills) if skills else "—"
    ide_label = execution_backend_label(ide)
    return (
        f"# {title}\n\n"
        f"- task_id: `{task_id}`\n"
        f"- coluna atual: `{column}`\n"
        f"- prioridade: `{priority}`\n"
        f"- assignees: {assignee_text}\n"
        f"- skills: {skills_text}\n"
        f"- workdir: `{workdir}`\n"
        f"- kanban_file: `{kanban_file}`\n\n"
        "## Descricao\n\n"
        f"{description or '_(sem descricao)_'}\n\n"
        "## Criterios de aceite\n\n"
        f"{acceptance or '_(nao informado)_'}\n\n"
        "## Definition of Done\n\n"
        "- [ ] Mudancas no escopo do card (sem refactor extra).\n"
        "- [ ] Testes relevantes passando (`.venv/bin/pytest -q` e/ou `npm test`).\n"
        "- [ ] Lint limpo (`ruff check src tests`).\n"
        "- [ ] Sem segredos no diff.\n"
        "- [ ] Arquivos novos no lugar certo (src/tests/docs/config/scripts).\n"
        f"- [ ] `complete_task` com result_comment citando arquivos + testes + IDE: {ide_label}.\n\n"
        "## Fechar o card\n\n"
        "```bash\n"
        f'curl -s -X POST "{base_url.rstrip("/")}/hermes/kanban" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        "  -d '{\n"
        f'    "workdir": "{workdir}",\n'
        f'    "kanban_file": "{kanban_file}",\n'
        '    "action": "complete_task",\n'
        f'    "task_id": "{task_id}",\n'
        f'    "result_comment": "<arquivos> | <testes> | IDE: {ide_label}"\n'
        "  }' | jq\n"
        "```\n"
    )


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def materialize_ide_context(
    *,
    repo_root: Path,
    ide: str,
    task: dict[str, Any],
    workdir: str,
    kanban_file: str,
    base_url: str = "http://127.0.0.1:9101",
) -> list[str]:
    """Write IDE-specific context files; return relative paths created."""
    backend = normalize_execution_backend(ide)
    if backend not in IDE_EXECUTION_BACKENDS:
        raise ValueError(f"unsupported IDE backend: {ide}")

    task_id = str(task.get("id") or task.get("task_id") or "").strip()
    title = str(task.get("title") or task_id).strip()
    if not task_id:
        raise ValueError("task_id is required")

    description = str(
        task.get("description") or task.get("text") or task.get("body") or ""
    ).strip()
    column = str(task.get("column") or "todo").strip()
    priority = task.get("priority", "")
    assignees_raw = task.get("assignees") or task.get("assignee") or []
    if isinstance(assignees_raw, str):
        assignees = [a.strip() for a in assignees_raw.split(",") if a.strip()]
    elif isinstance(assignees_raw, list):
        assignees = [str(a).strip() for a in assignees_raw if str(a).strip()]
    else:
        assignees = []
    skills_raw = task.get("skills") or []
    if isinstance(skills_raw, str):
        skills = [s.strip() for s in re.split(r"[\n,;]", skills_raw) if s.strip()]
    elif isinstance(skills_raw, list):
        skills = [str(s).strip() for s in skills_raw if str(s).strip()]
    else:
        skills = []
    acceptance = str(
        task.get("acceptance") or task.get("acceptance_criteria") or ""
    ).strip()
    slug = _slug_from_task(task_id, title)
    common = _common_markdown(
        task_id=task_id,
        title=title,
        description=description,
        column=column,
        priority=priority,
        assignees=assignees,
        skills=skills,
        workdir=workdir,
        kanban_file=kanban_file,
        acceptance=acceptance,
        ide=backend,
        base_url=base_url,
    )

    root = repo_root.resolve()
    created: list[str] = []

    if backend == "kiro":
        spec_dir = root / ".kiro" / "specs" / slug
        for name, body in (
            (
                "requirements.md",
                common
                + "\n## Requisitos (EARS)\n\n"
                "- Enquanto `<contexto>`, quando `<evento>`, o sistema deve `<comportamento>`.\n",
            ),
            (
                "design.md",
                f"# Design — {title} (`{task_id}`)\n\n"
                "## Arquitetura\n- componentes afetados:\n\n"
                "## Verificacao\n- testes a rodar:\n",
            ),
            (
                "tasks.md",
                f"# Tasks — {title} (`{task_id}`)\n\n"
                "- [ ] 1. Investigar arquivos relevantes\n"
                "- [ ] 2. Patch incremental\n"
                "- [ ] 3. Testes\n"
                "- [ ] 4. complete_task\n",
            ),
        ):
            target = spec_dir / name
            _write_text(target, body)
            created.append(str(target.relative_to(root)))
    elif backend == "cursor":
        target = root / ".cursor" / "rules" / f"qclaw-card-{task_id}.mdc"
        body = (
            "---\n"
            f"description: Contexto do card {task_id}\n"
            "globs:\n"
            "alwaysApply: false\n"
            "---\n\n"
            f"{common}\n"
            "## Instrucoes para o Composer\n\n"
            "1. Leia os arquivos relevantes antes de patchar.\n"
            "2. Siga CLAUDE.md (sem arquivos no root, < 500 linhas).\n"
        )
        _write_text(target, body)
        created.append(str(target.relative_to(root)))
    elif backend in {"vscode", "copilot"}:
        sub = "copilot" if backend == "copilot" else "qclaw-prompts"
        target = root / ".vscode" / sub / f"{task_id}.md"
        extra = ""
        if backend == "copilot":
            extra = (
                "\n## Instrucoes para Copilot Chat\n\n"
                "- Use o chat do Copilot com este arquivo como contexto.\n"
                "- Rode testes antes de concluir o card.\n"
            )
        _write_text(target, common + extra)
        created.append(str(target.relative_to(root)))
    elif backend == "antigravity":
        target = root / ".antigravity" / "prompts" / f"{task_id}.md"
        body = (
            f"{common}\n"
            "## Observacoes para Antigravity\n\n"
            "- Use para tarefas com navegacao web/API ou validacao visual.\n"
        )
        _write_text(target, body)
        created.append(str(target.relative_to(root)))

    return created


def open_ide_cli(ide: str, repo_root: Path) -> dict[str, Any]:
    """Best-effort launch of the IDE CLI for ``repo_root``."""
    backend = normalize_execution_backend(ide)
    candidates = _IDE_CLI.get(backend, ())
    for binary in candidates:
        if shutil.which(binary):
            try:
                subprocess.Popen(  # noqa: S603
                    [binary, str(repo_root.resolve())],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return {"ok": True, "cli": binary, "opened": True}
            except OSError as exc:
                return {"ok": False, "cli": binary, "opened": False, "error": str(exc)}
    return {
        "ok": True,
        "cli": candidates[0] if candidates else "",
        "opened": False,
        "error": f"CLI não instalada — abra manualmente: {repo_root}",
    }


def run_kanban_ide_handoff(
    *,
    repo_root: Path,
    ide: str,
    task: dict[str, Any],
    workdir: str,
    kanban_file: str,
    base_url: str = "http://127.0.0.1:9101",
    open_ide: bool = True,
) -> dict[str, Any]:
    """Materialize IDE context and optionally launch the IDE."""
    backend = normalize_execution_backend(ide)
    if not is_ide_execution_backend(backend):
        return {"ok": False, "error": f"backend '{backend}' não é uma IDE"}

    try:
        files = materialize_ide_context(
            repo_root=repo_root,
            ide=backend,
            task=task,
            workdir=workdir,
            kanban_file=kanban_file,
            base_url=base_url,
        )
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    launch: dict[str, Any] = {"opened": False}
    if open_ide:
        launch = open_ide_cli(backend, repo_root)

    return {
        "ok": True,
        "mode": "ide_handoff",
        "execution_backend": backend,
        "execution_backend_label": execution_backend_label(backend),
        "context_files": files,
        "ide_launch": launch,
        "message": (
            f"Handoff para {execution_backend_label(backend)} — "
            "implemente na IDE e feche o card com complete_task."
        ),
    }
