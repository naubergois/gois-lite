"""Diretório de trabalho (workspace) de cada time.

Cada time possui um workspace dedicado em:
    .stack/accounts/teams/<team-id>/workspace/

Estrutura padrão:
    workspace/
    ├── artifacts/       → Artefatos gerados (relatórios, builds, exports)
    ├── docs/            → Documentação do time (specs, ADRs, runbooks)
    ├── reports/         → Relatórios de análise, métricas, auditorias
    ├── code/            → Código-fonte ou patches produzidos pelo time
    ├── data/            → Datasets, dumps, arquivos de entrada/saída
    ├── models/          → Modelos treinados, pesos, checkpoints
    ├── figures/         → Imagens, diagramas, gráficos gerados
    ├── logs/            → Logs de execução de agentes e jobs do time
    └── tmp/             → Arquivos temporários (limpos periodicamente)

Todos os artefatos gerados por agentes, jobs ou comandos associados a um
time devem ser direcionados ao workspace do respectivo time.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Subdiretórios padrão criados em cada workspace de time.
WORKSPACE_SUBDIRS: list[dict[str, str]] = [
    {"name": "artifacts", "description": "Artefatos gerados (relatórios, builds, exports)"},
    {"name": "docs", "description": "Documentação do time (specs, ADRs, runbooks)"},
    {"name": "reports", "description": "Relatórios de análise, métricas, auditorias"},
    {"name": "code", "description": "Código-fonte ou patches produzidos pelo time"},
    {"name": "data", "description": "Datasets, dumps, arquivos de entrada/saída"},
    {"name": "models", "description": "Modelos treinados, pesos, checkpoints"},
    {"name": "figures", "description": "Imagens, diagramas, gráficos gerados"},
    {"name": "logs", "description": "Logs de execução de agentes e jobs do time"},
    {"name": "tmp", "description": "Arquivos temporários (limpos periodicamente)"},
]

WORKSPACE_DIR_NAME = "workspace"


class TeamWorkspace:
    """Gerencia o diretório de trabalho de um time."""

    def __init__(self, team_dir: Path, team_id: str):
        self.team_dir = team_dir
        self.team_id = team_id
        self.root = team_dir / WORKSPACE_DIR_NAME

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def docs(self) -> Path:
        return self.root / "docs"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def code(self) -> Path:
        return self.root / "code"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def figures(self) -> Path:
        return self.root / "figures"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    def exists(self) -> bool:
        """Verifica se o workspace já foi inicializado."""
        return self.root.is_dir()

    def initialize(self) -> None:
        """Cria a estrutura completa de diretórios do workspace."""
        for subdir_info in WORKSPACE_SUBDIRS:
            subdir = self.root / subdir_info["name"]
            subdir.mkdir(parents=True, exist_ok=True)
            # Cria .gitkeep para manter diretórios vazios no Git
            gitkeep = subdir / ".gitkeep"
            if not gitkeep.exists():
                gitkeep.touch()
        # Cria README.md no workspace com informação do time
        self._write_readme()
        log.info("workspace inicializado para time %s em %s", self.team_id, self.root)

    def _write_readme(self) -> None:
        """Cria um README.md no workspace descrevendo a estrutura."""
        readme_path = self.root / "README.md"
        lines = [
            f"# Workspace — {self.team_id}\n",
            "",
            "Diretório de trabalho do time. Todos os artefatos gerados devem ir aqui.\n",
            "",
            "## Estrutura\n",
            "",
            "| Diretório | Descrição |",
            "|-----------|-----------|",
        ]
        for sd in WORKSPACE_SUBDIRS:
            lines.append(f"| `{sd['name']}/` | {sd['description']} |")
        lines.extend([
            "",
            "## Regras\n",
            "",
            "- Agentes e jobs do time devem salvar saídas neste workspace.",
            "- O diretório `tmp/` pode ser limpo periodicamente.",
            "- Artefatos finais devem ir em `artifacts/`.",
            "- Relatórios de execução e métricas devem ir em `reports/`.",
            f"- Criado em: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "",
        ])
        readme_path.write_text("\n".join(lines), encoding="utf-8")

    def resolve_path(self, subdir: str, filename: str) -> Path:
        """Resolve um caminho seguro dentro do workspace.

        Previne path traversal — o resultado estará sempre dentro de self.root.
        """
        safe_subdir = Path(subdir)
        if safe_subdir.is_absolute() or ".." in safe_subdir.parts:
            raise ValueError(f"subdiretório inválido: {subdir!r}")
        safe_file = Path(filename)
        if safe_file.is_absolute() or ".." in safe_file.parts:
            raise ValueError(f"nome de arquivo inválido: {filename!r}")
        target = self.root / safe_subdir / safe_file
        # Garante que o resultado está dentro do workspace
        target.resolve().relative_to(self.root.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def save_artifact(
        self,
        content: str,
        filename: str,
        *,
        subdir: str = "artifacts",
        metadata: Optional[dict[str, Any]] = None,
    ) -> Path:
        """Salva um artefato no workspace e retorna o caminho."""
        target = self.resolve_path(subdir, filename)
        target.write_text(content, encoding="utf-8")
        log.info("artefato salvo: %s (time=%s)", target, self.team_id)
        return target

    def list_artifacts(self, subdir: str = "artifacts") -> list[dict[str, Any]]:
        """Lista artefatos de um subdiretório do workspace."""
        safe_subdir = Path(subdir)
        if safe_subdir.is_absolute() or ".." in safe_subdir.parts:
            raise ValueError(f"subdiretório inválido: {subdir!r}")
        target_dir = self.root / safe_subdir
        if not target_dir.is_dir():
            return []
        results: list[dict[str, Any]] = []
        for item in sorted(target_dir.iterdir()):
            if item.name.startswith("."):
                continue
            stat = item.stat()
            results.append({
                "name": item.name,
                "path": str(item),
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "is_dir": item.is_dir(),
            })
        return results

    def get_summary(self) -> dict[str, Any]:
        """Retorna um resumo do workspace (contagem de arquivos por subdir)."""
        summary: dict[str, Any] = {
            "team_id": self.team_id,
            "root": str(self.root),
            "exists": self.exists(),
            "subdirs": {},
        }
        if not self.exists():
            return summary
        for sd_info in WORKSPACE_SUBDIRS:
            sd_path = self.root / sd_info["name"]
            if sd_path.is_dir():
                files = [f for f in sd_path.iterdir() if not f.name.startswith(".")]
                summary["subdirs"][sd_info["name"]] = {
                    "description": sd_info["description"],
                    "file_count": len(files),
                    "total_size": sum(f.stat().st_size for f in files if f.is_file()),
                }
            else:
                summary["subdirs"][sd_info["name"]] = {
                    "description": sd_info["description"],
                    "file_count": 0,
                    "total_size": 0,
                }
        return summary

    def clean_tmp(self) -> int:
        """Remove todos os arquivos de tmp/. Retorna quantos foram removidos."""
        count = 0
        if self.tmp.is_dir():
            for item in self.tmp.iterdir():
                if item.name.startswith("."):
                    continue
                if item.is_file():
                    item.unlink()
                    count += 1
                elif item.is_dir():
                    shutil.rmtree(item)
                    count += 1
        return count


def get_team_workspace(teams_base_dir: Path, team_id: str) -> TeamWorkspace:
    """Factory — obtém o TeamWorkspace para um dado time.

    Args:
        teams_base_dir: diretório base dos times (.stack/accounts/teams/)
        team_id: identificador do time
    """
    team_dir = teams_base_dir / team_id
    if not team_dir.is_dir():
        raise ValueError(f"diretório do time não encontrado: {team_dir}")
    return TeamWorkspace(team_dir=team_dir, team_id=team_id)


def ensure_team_workspace(teams_base_dir: Path, team_id: str) -> TeamWorkspace:
    """Garante que o workspace do time existe, criando se necessário.

    Args:
        teams_base_dir: diretório base dos times (.stack/accounts/teams/)
        team_id: identificador do time
    """
    team_dir = teams_base_dir / team_id
    team_dir.mkdir(parents=True, exist_ok=True)
    ws = TeamWorkspace(team_dir=team_dir, team_id=team_id)
    if not ws.exists():
        ws.initialize()
    return ws


def initialize_all_team_workspaces(teams_base_dir: Path) -> list[str]:
    """Inicializa workspaces para todos os times existentes que ainda não têm.

    Returns:
        Lista de team_ids cujos workspaces foram criados.
    """
    if not teams_base_dir.is_dir():
        return []
    initialized: list[str] = []
    for team_dir in sorted(teams_base_dir.iterdir()):
        if not team_dir.is_dir():
            continue
        team_id = team_dir.name
        ws = TeamWorkspace(team_dir=team_dir, team_id=team_id)
        if not ws.exists():
            ws.initialize()
            initialized.append(team_id)
    return initialized
