"""Safe staging and download of chat-generated artifacts (ZIP, decks, etc.)."""

from __future__ import annotations

import mimetypes
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from .chat_models import _safe_filename
from .local_paths import media_chat_subdir, project_stack_root


def resolve_artifacts_dir() -> Path:
    return media_chat_subdir("artifacts")


def resolve_previews_dir() -> Path:
    """Writable dir for rendered PDF/slide preview PNGs (chat download)."""
    return media_chat_subdir("previews")


def _allowed_roots() -> list[Path]:
    roots = [
        resolve_artifacts_dir(),
        resolve_previews_dir(),
        media_chat_subdir("attachments"),
        media_chat_subdir("replicate"),
    ]
    try:
        from .roteiro_viral_local.config import local_output_root

        roots.append(local_output_root().resolve())
    except OSError:
        pass
    try:
        from .local_paths import _repo_root

        repo_output = (_repo_root() / "output").resolve()
        repo_output.mkdir(parents=True, exist_ok=True)
        roots.append(repo_output)
    except OSError:
        pass
    try:
        rv_output = (Path(__file__).resolve().parent / "roteiro_viral" / "output").resolve()
        rv_output.mkdir(parents=True, exist_ok=True)
        roots.append(rv_output)
    except OSError:
        pass
    from .visual_memory import storage_root as vm_storage_root

    try:
        roots.append(vm_storage_root().resolve())
    except OSError:
        pass
    tmp = (project_stack_root().parent / "tmp").resolve()
    tmp.mkdir(parents=True, exist_ok=True)
    roots.append(tmp)
    try:
        roots.append(Path(tempfile.gettempdir()).resolve())
    except OSError:
        pass
    try:
        roots.append(Path("/tmp").resolve())
    except OSError:
        pass
    downloads = (project_stack_root() / "downloads").resolve()
    downloads.mkdir(parents=True, exist_ok=True)
    roots.append(downloads)
    teams = (project_stack_root() / "accounts" / "teams").resolve()
    teams.mkdir(parents=True, exist_ok=True)
    roots.append(teams)
    return roots


def is_downloadable_artifact(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False
    if not resolved.is_file():
        return False
    for root in _allowed_roots():
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def artifact_download_url(path: Path, *, base_url: str = "") -> str:
    """Build a download URL for a staged artifact."""
    resolved = path.expanduser().resolve()
    prefix = (base_url or "").rstrip("/")
    q = quote(str(resolved), safe="")
    return f"{prefix}/openclaw/artifacts/download?path={q}"


def read_artifact_bytes(path: str) -> tuple[bytes, str, str]:
    """Return (bytes, mime_type, filename) for a validated artifact path."""
    src = Path(str(path).strip()).expanduser()
    if not is_downloadable_artifact(src):
        raise FileNotFoundError(f"artifact not allowed or missing: {src}")
    data = src.read_bytes()
    from .image_file_utils import mime_type_for_path

    mime = mime_type_for_path(src, data=data[:16])
    return data, mime, src.name


def stage_artifact(
    src_path: Path,
    *,
    label: Optional[str] = None,
    session_key: str = "",
) -> dict[str, Any]:
    """Copy *src_path* into the artifacts dir and return attachment metadata."""
    src = src_path.expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"source not found: {src}")

    artifacts = resolve_artifacts_dir()
    session_slug = re.sub(r"[^\w\-.]+", "_", (session_key or "").strip())[:64]
    if session_slug:
        dest_dir = artifacts / session_slug
    else:
        dest_dir = artifacts / f"batch-{int(time.time())}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    name = _safe_filename(label or src.name)
    dest = dest_dir / name
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        dest = dest_dir / f"{stem}-{int(time.time())}{suffix}"
    shutil.copy2(src, dest)

    mime, _ = mimetypes.guess_type(dest.name)
    if not mime:
        mime = "application/octet-stream"
    size = dest.stat().st_size
    return {
        "name": dest.name,
        "path": str(dest),
        "mime_type": mime,
        "size": size,
        "download_url": artifact_download_url(dest),
    }
