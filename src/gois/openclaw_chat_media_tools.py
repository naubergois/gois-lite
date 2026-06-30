"""Media attachment helpers for qclaw_show_media chat tool."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from .slides_pdf_preview import page_number_from_path, pdf_page_count, render_document_pages

log = logging.getLogger(__name__)

_MEDIA_IMAGE_FIELDS = ("image_data_url", "image_url")
_MEDIA_VIDEO_FIELDS = ("video_url", "video_data_url")
_MEDIA_TOTAL_LIMIT = 12

# MongoDB BSON document limit is 16 MB; multiple inline images can exceed it.
_PERSIST_DATA_URL_MAX_CHARS = 512 * 1024  # keep small payloads (QR codes, etc.)
_PERSIST_EXTRAS_JSON_MAX_CHARS = 4 * 1024 * 1024
_PERSIST_JOB_DOC_MAX_BYTES = 14 * 1024 * 1024  # headroom under 16 MB BSON cap


def _safe_media_url(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    if s.startswith(
        (
            "data:image/",
            "data:video/",
            "https://",
            "http://",
            "blob:",
            "/openclaw/",
            "/hermes/",
            "/latex/",
        )
    ):
        return s
    return ""


_IMAGE_PATH_KEYS = (
    "image_path",
    "path",
    "file_path",
    "output_path",
    "thumbnail_path",
    "png_path",
)
_NESTED_MEDIA_KEYS = ("result", "final_state", "output", "response", "data", "parsed")
_SCENE_ARRAY_KEYS = (
    "images",
    "videos",
    "scenes",
    "storyboard",
    "storyboard_images",
    "acts",
    "visual_plan",
    "slides",
    "generated",
)
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg")


def _looks_like_image_path(path: str) -> bool:
    low = str(path or "").strip().lower()
    return bool(low) and low.endswith(_IMAGE_SUFFIXES)


def _media_caption_from_item(item: dict[str, Any]) -> str:
    for key in (
        "caption",
        "title",
        "segment",
        "act_name",
        "name",
        "label",
        "scene",
        "act",
    ):
        val = str(item.get(key) or "").strip()
        if val:
            return val
    return ""


def _push_image_from_path_item(
    media_extra: dict[str, list[dict[str, Any]]],
    item: dict[str, Any],
) -> None:
    if _media_total(media_extra) >= _MEDIA_TOTAL_LIMIT:
        return
    path_raw = ""
    for key in _IMAGE_PATH_KEYS:
        candidate = str(item.get(key) or "").strip()
        if candidate and _looks_like_image_path(candidate):
            path_raw = candidate
            break
    if not path_raw:
        return
    caption = _media_caption_from_item(item)
    entry = _hydrate_image_item(
        {
            "image_path": path_raw,
            "caption": caption or None,
            "download_url": item.get("download_url"),
        }
    )
    src = _safe_media_url(
        entry.get("image_data_url")
        or entry.get("image_url")
        or entry.get("download_url")
        or item.get("download_url")
    )
    if not src:
        return
    _push_image(
        media_extra,
        src,
        caption=entry.get("caption") or caption,
        image_path=entry.get("image_path") or path_raw,
        download_url=entry.get("download_url") or item.get("download_url"),
    )


def _media_total(media_extra: dict[str, list[dict[str, Any]]]) -> int:
    return len(media_extra.get("images") or []) + len(media_extra.get("videos") or [])


def has_persistable_chat_media(media_extra: Optional[dict[str, Any]]) -> bool:
    """True when tool output includes inline or path-backed chat media."""
    if not isinstance(media_extra, dict):
        return False
    return bool(
        media_extra.get("images")
        or media_extra.get("videos")
        or media_extra.get("preview_fallback")
        or media_extra.get("templatePreview")
        or media_extra.get("miniCurriculumPreview")
        or media_extra.get("creativeWidget")
    )


def build_chat_media_out(media_extra: dict[str, Any]) -> dict[str, Any]:
    """Build the ``media`` payload returned to the dashboard chat UI."""
    out: dict[str, Any] = {
        "images": media_extra.get("images") or [],
        "videos": media_extra.get("videos") or [],
    }
    if media_extra.get("templatePreview"):
        out["templatePreview"] = media_extra["templatePreview"]
    if media_extra.get("miniCurriculumPreview"):
        out["miniCurriculumPreview"] = media_extra["miniCurriculumPreview"]
    if media_extra.get("creativeWidget"):
        out["creativeWidget"] = media_extra["creativeWidget"]
    if media_extra.get("roteiroLabOpen"):
        out["roteiroLabOpen"] = media_extra["roteiroLabOpen"]
    if media_extra.get("bookEditorOpen"):
        out["bookEditorOpen"] = media_extra["bookEditorOpen"]
    if media_extra.get("bookCoverEditorOpen"):
        out["bookCoverEditorOpen"] = media_extra["bookCoverEditorOpen"]
    if media_extra.get("socialPostEditorOpen"):
        out["socialPostEditorOpen"] = media_extra["socialPostEditorOpen"]
    if media_extra.get("preview_fallback"):
        out["preview_fallback"] = True
        for key in ("image_path", "video_path", "caption", "message"):
            if media_extra.get(key):
                out[key] = media_extra[key]
    hydrated = hydrate_media_payload_from_paths(out)
    return hydrated if isinstance(hydrated, dict) else out


def media_payload_from_job_id(job_id: Optional[str]) -> Optional[dict[str, Any]]:
    """Hydrate partial media captured during an async chat send job."""
    jid = (job_id or "").strip()
    if not jid:
        return None
    from .chat_jobs import get_job, should_defer_image_batch_chat_preview

    job = get_job(jid)
    if job is None or not job.partial_media:
        return None
    if should_defer_image_batch_chat_preview(job):
        return None
    hydrated = hydrate_media_payload_from_paths(job.partial_media)
    if not isinstance(hydrated, dict):
        return None
    if not (hydrated.get("images") or hydrated.get("videos")):
        return None
    return hydrated


def _image_download_url_from_path(path_raw: str) -> str | None:
    path_raw = str(path_raw or "").strip()
    if not path_raw:
        return None
    try:
        from .chat_artifacts import artifact_download_url, is_downloadable_artifact

        path = Path(path_raw).expanduser().resolve()
        if is_downloadable_artifact(path):
            return artifact_download_url(path)
    except Exception:
        pass
    try:
        from .team_files_download import resolve_team_file, team_file_download_href

        resolved = resolve_team_file(path=path_raw)
        if resolved.get("ok"):
            return team_file_download_href(
                team_id=str(resolved.get("team_id") or ""),
                team_name=str(resolved.get("team_name") or ""),
                relative_path=str(resolved.get("relative_path") or ""),
                path=str(resolved.get("path") or path_raw),
            )
    except Exception:
        pass
    return None


def _attach_image_download_url(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("download_url"):
        return item
    path_raw = str(item.get("image_path") or item.get("path") or "").strip()
    if not path_raw:
        return item
    url = _image_download_url_from_path(path_raw)
    if not url:
        return item
    out = dict(item)
    out["download_url"] = url
    return out


def _push_image(
    media_extra: dict[str, list[dict[str, Any]]],
    src: str,
    *,
    caption: Any = "",
    image_path: Any = "",
    download_url: Any = "",
) -> None:
    if not src or _media_total(media_extra) >= _MEDIA_TOTAL_LIMIT:
        return
    entry: dict[str, Any] = {
        "image_data_url": src,
        "caption": str(caption or "") or None,
        "image_path": str(image_path or "") or None,
    }
    url = str(download_url or "").strip() or _image_download_url_from_path(
        str(image_path or "")
    )
    if url:
        entry["download_url"] = url
    media_extra.setdefault("images", []).append(entry)


def _push_video(
    media_extra: dict[str, list[dict[str, Any]]],
    src: str,
    *,
    poster: Any = "",
    caption: Any = "",
    video_path: Any = "",
    duration_s: Any = None,
) -> None:
    if not src or _media_total(media_extra) >= _MEDIA_TOTAL_LIMIT:
        return
    media_extra.setdefault("videos", []).append(
        {
            "video_url": src,
            "poster_data_url": _safe_media_url(poster) or None,
            "caption": str(caption or "") or None,
            "video_path": str(video_path or "") or None,
            "duration_s": duration_s if isinstance(duration_s, (int, float)) else None,
        }
    )


def _has_media_path(item: dict[str, Any], *, kind: str) -> bool:
    if kind == "image":
        return bool(
            str(item.get("image_path") or item.get("path") or "").strip()
            or str(item.get("image_url") or item.get("url") or "").strip()
        )
    return bool(
        str(item.get("video_path") or item.get("path") or "").strip()
        or str(item.get("video_url") or item.get("url") or "").strip()
    )


def _strip_data_url_field(
    item: dict[str, Any],
    field: str,
    *,
    kind: str,
) -> None:
    value = item.get(field)
    if not isinstance(value, str) or not value.startswith("data:"):
        return
    if len(value) <= _PERSIST_DATA_URL_MAX_CHARS and not _has_media_path(item, kind=kind):
        return
    if _has_media_path(item, kind=kind) or len(value) > _PERSIST_DATA_URL_MAX_CHARS:
        item.pop(field, None)
        if not _has_media_path(item, kind=kind):
            item.setdefault(
                "preview_fallback",
                True,
            )
            item.setdefault(
                "message",
                "Mídia omitida do histórico (payload grande). Use o caminho local ou regenere.",
            )


def _sanitize_media_dict(media: dict[str, Any]) -> dict[str, Any]:
    out = dict(media)
    images: list[dict[str, Any]] = []
    for raw in out.get("images") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        for fld in _MEDIA_IMAGE_FIELDS:
            _strip_data_url_field(item, fld, kind="image")
        images.append(item)
    out["images"] = images

    videos: list[dict[str, Any]] = []
    for raw in out.get("videos") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        for fld in _MEDIA_VIDEO_FIELDS:
            _strip_data_url_field(item, fld, kind="video")
        _strip_data_url_field(item, "poster_data_url", kind="image")
        videos.append(item)
    out["videos"] = videos

    for fld in _MEDIA_IMAGE_FIELDS:
        _strip_data_url_field(out, fld, kind="image")
    for fld in _MEDIA_VIDEO_FIELDS:
        _strip_data_url_field(out, fld, kind="video")
    _strip_data_url_field(out, "poster_data_url", kind="image")
    return out


def _sanitize_nested_data_urls(obj: Any) -> Any:
    """Strip large data URLs from wacli QR / desktop screenshot blocks."""
    if isinstance(obj, dict):
        out = dict(obj)
        for key, value in list(out.items()):
            if isinstance(value, str) and value.startswith("data:"):
                if len(value) > _PERSIST_DATA_URL_MAX_CHARS:
                    out.pop(key, None)
            else:
                out[key] = _sanitize_nested_data_urls(value)
        return out
    if isinstance(obj, list):
        return [_sanitize_nested_data_urls(item) for item in obj]
    return obj


def attachments_meta_for_persistence(raw: Any) -> list[dict[str, Any]]:
    """Persist attachment metadata (path, name, mime) without inline blobs."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {
            "name": str(item.get("name") or "anexo"),
            "mime_type": str(
                item.get("mime_type") or item.get("mime") or "application/octet-stream"
            ),
        }
        size = item.get("size")
        if size is not None:
            try:
                row["size"] = int(size)
            except (TypeError, ValueError):
                pass
        path = str(item.get("path") or "").strip()
        if path:
            row["path"] = path
        out.append(row)
    return out


def _sanitize_attachments_list(items: Any) -> list[dict[str, Any]]:
    return attachments_meta_for_persistence(items)


def sanitize_extras_for_persistence(
    extras: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Shrink chat extras before MongoDB insert (avoid 16 MB BSON limit)."""
    if not extras:
        return extras
    out = dict(extras)
    media = out.get("media")
    if isinstance(media, dict):
        out["media"] = _sanitize_media_dict(media)
    attachments = out.get("attachments")
    if isinstance(attachments, list):
        out["attachments"] = _sanitize_attachments_list(attachments)
    for key in ("wacliQr", "desktopScreenshot"):
        block = out.get(key)
        if isinstance(block, dict):
            out[key] = _sanitize_nested_data_urls(block)
    encoded = json.dumps(out, ensure_ascii=False, default=str)
    if len(encoded) <= _PERSIST_EXTRAS_JSON_MAX_CHARS:
        return out
    # Last resort: drop every remaining data URL.
    return _sanitize_nested_data_urls(out)


def _sanitize_image_dict_list(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        for fld in _MEDIA_IMAGE_FIELDS:
            _strip_data_url_field(item, fld, kind="image")
        out.append(item)
    return out


def sanitize_job_result_for_persistence(
    result: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Shrink completed job results before MongoDB replace (16 MB BSON guard)."""
    if not result:
        return result
    out = _sanitize_nested_data_urls(dict(result))
    if isinstance(out.get("media"), dict):
        out["media"] = _sanitize_media_dict(out["media"])
    if isinstance(out.get("images"), list):
        out["images"] = _sanitize_image_dict_list(out["images"])
    generated = out.get("generated")
    if isinstance(generated, list):
        out["generated"] = _sanitize_image_dict_list(generated)
    for key in ("stdout", "stderr"):
        value = out.get(key)
        if isinstance(value, str) and len(value) > 8000:
            out[key] = value[:8000] + "…"
    return out


def sanitize_chat_job_snapshot_for_persistence(
    *,
    partial_media: Optional[dict[str, Any]],
    result: Optional[dict[str, Any]],
    kind: str = "",
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Prepare chat job media/result fields for MongoDB replace_one."""
    media = sanitize_media_payload_for_persistence(partial_media)
    if media and (kind or "").strip() == "image_batch":
        images = media.get("images") or []
        if len(images) > _MEDIA_TOTAL_LIMIT:
            media = dict(media)
            media["images"] = images[-_MEDIA_TOTAL_LIMIT:]
    res = sanitize_job_result_for_persistence(result)
    return media, res


def cap_streaming_job_media(
    media: dict[str, Any],
    *,
    kind: str = "",
    max_images: int = _MEDIA_TOTAL_LIMIT,
) -> dict[str, Any]:
    """Keep in-memory/streaming job media bounded (paths only, recent previews)."""
    out = sanitize_media_payload_for_persistence(media) or dict(media)
    images = out.get("images") or []
    if isinstance(images, list) and len(images) > max(1, int(max_images)):
        out = dict(out)
        out["images"] = images[-max(1, int(max_images)) :]
    if (kind or "").strip() == "image_batch":
        out = sanitize_media_payload_for_persistence(out) or out
    return out


def _job_doc_encoded_size(doc: dict[str, Any]) -> int:
    return len(json.dumps(doc, ensure_ascii=False, default=str).encode("utf-8"))


def ensure_job_doc_fits_bson(doc: dict[str, Any]) -> dict[str, Any]:
    """Shrink a chat job Mongo document before replace_one (16 MB BSON guard)."""
    out = dict(doc)
    kind = str(out.get("kind") or "")
    partial = out.get("partial_media")
    if isinstance(partial, dict):
        out["partial_media"] = cap_streaming_job_media(partial, kind=kind)
    result = out.get("result")
    if isinstance(result, dict):
        out["result"] = sanitize_job_result_for_persistence(result)
    if _job_doc_encoded_size(out) <= _PERSIST_JOB_DOC_MAX_BYTES:
        return out

    for mirror in ("partial_media_json", "result_json", "progress_json"):
        out.pop(mirror, None)
    if _job_doc_encoded_size(out) <= _PERSIST_JOB_DOC_MAX_BYTES:
        return out

    out = _sanitize_nested_data_urls(out)
    if _job_doc_encoded_size(out) <= _PERSIST_JOB_DOC_MAX_BYTES:
        return out

    partial = out.get("partial_media")
    if isinstance(partial, dict):
        slim = cap_streaming_job_media(partial, kind=kind)
        out["partial_media"] = slim
        out.pop("partial_media_json", None)
    result = out.get("result")
    if isinstance(result, dict):
        out["result"] = sanitize_job_result_for_persistence(result)
        out.pop("result_json", None)

    if _job_doc_encoded_size(out) <= _PERSIST_JOB_DOC_MAX_BYTES:
        return out

    return _sanitize_nested_data_urls(out)


def _image_item_has_displayable_src(item: dict[str, Any]) -> bool:
    """True when the chat UI can render an inline preview (not a phantom path)."""
    if _safe_media_url(item.get("image_data_url") or item.get("image_url")):
        return True
    return bool(_safe_media_url(item.get("download_url")))


def _hydrate_image_item(item: dict[str, Any]) -> dict[str, Any]:
    if _safe_media_url(item.get("image_data_url") or item.get("image_url")):
        return _attach_image_download_url(dict(item))
    path_raw = str(item.get("image_path") or item.get("path") or "").strip()
    if not path_raw:
        return item
    out = dict(item)
    try:
        path = Path(path_raw).expanduser().resolve()
        if not path.is_file():
            out.setdefault("preview_fallback", True)
            out.setdefault(
                "message",
                "Ficheiro de imagem não encontrado no disco.",
            )
            return out
        size = path.stat().st_size
        if size > _HYDRATE_FILE_MAX_BYTES:
            out.setdefault("preview_fallback", True)
            out.setdefault(
                "message",
                f"Imagem com {size} bytes — abra no Preview pelo caminho guardado.",
            )
            return _attach_image_download_url(out)
        data_url, _ = _show_media_to_data_url(path, prefer_kind="image")
        out["image_data_url"] = data_url
    except OSError:
        out.setdefault("preview_fallback", True)
        out.setdefault(
            "message",
            "Ficheiro de imagem não encontrado no disco.",
        )
        return out
    return _attach_image_download_url(out)


def hydrate_media_payload_light(
    media: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Fast poll hydration: preserve inline URLs, attach download links — no disk reads."""
    if not media or not isinstance(media, dict):
        return media
    out = dict(media)
    images = out.get("images") or []
    if isinstance(images, list):
        out["images"] = [
            _attach_image_download_url(dict(item))
            for item in images
            if isinstance(item, dict)
        ]
    for fld in _MEDIA_IMAGE_FIELDS:
        if _safe_media_url(out.get(fld)):
            continue
        path_raw = str(out.get("image_path") or "").strip()
        if not path_raw:
            continue
        url = _image_download_url_from_path(path_raw)
        if url:
            out["download_url"] = url
    if out.get("image_path") and not out.get("download_url"):
        url = _image_download_url_from_path(str(out["image_path"]))
        if url:
            out["download_url"] = url
    return out


def hydrate_media_payload_from_paths(
    media: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Re-attach inline data URLs from disk paths (job status / streaming)."""
    if not media or not isinstance(media, dict):
        return media
    out = dict(media)
    images = out.get("images") or []
    if isinstance(images, list):
        hydrated = [
            _hydrate_image_item(item)
            for item in images
            if isinstance(item, dict)
        ]
        displayable = [item for item in hydrated if _image_item_has_displayable_src(item)]
        dropped = len(hydrated) - len(displayable)
        out["images"] = displayable
        if dropped and not displayable and not out.get("preview_fallback"):
            out["preview_fallback"] = True
            out.setdefault(
                "message",
                f"{dropped} imagem(ns) indisponível(is) — ficheiro em falta ou fora do workspace.",
            )
    for fld in _MEDIA_IMAGE_FIELDS:
        if not _safe_media_url(out.get(fld)) and out.get("image_path"):
            try:
                path = Path(str(out["image_path"])).expanduser().resolve()
                if path.is_file():
                    data_url, _ = _show_media_to_data_url(path, prefer_kind="image")
                    out[fld] = data_url
            except OSError:
                pass
    if out.get("image_path"):
        top_url = _image_download_url_from_path(str(out["image_path"]))
        if top_url:
            out["download_url"] = top_url
    return out


def hydrate_attachments_from_paths(
    attachments: Any,
) -> list[dict[str, Any]]:
    """Ensure downloadable chat attachments expose ``download_url`` from disk paths."""
    if not isinstance(attachments, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in attachments:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        if not item.get("download_url"):
            path_raw = str(item.get("path") or "").strip()
            if path_raw:
                try:
                    from .chat_artifacts import artifact_download_url

                    item["download_url"] = artifact_download_url(Path(path_raw))
                except Exception:
                    pass
        out.append(item)
    return out


def hydrate_extras_from_paths(
    extras: dict[str, Any],
    *,
    light: bool = False,
) -> dict[str, Any]:
    """Re-attach inline data URLs from disk when loading chat history."""
    out = dict(extras)
    media = out.get("media")
    if isinstance(media, dict):
        hydrate = hydrate_media_payload_light if light else hydrate_media_payload_from_paths
        out["media"] = hydrate(media)
    if out.get("attachments"):
        out["attachments"] = hydrate_attachments_from_paths(out["attachments"])
    return out


def sanitize_media_payload_for_persistence(
    media: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Sanitize streaming job media before MongoDB replace."""
    if not media:
        return media
    return _sanitize_media_dict(media)


def _collect_media_from_tool_result(
    result: dict[str, Any],
    media_extra: dict[str, list[dict[str, Any]]],
    *,
    _seen: Optional[set[int]] = None,
) -> None:
    """Extract image/video media from a generic tool result into media_extra.

    Honors the contract from `skills/qclaw-chat-midia/SKILL.md`:
    - `image_data_url` / `image_url` (single image)
    - `video_url` / `video_data_url` (single video) + optional `poster_data_url`
    - `images: [...]` array, each with the same fields
    - `videos: [...]` array, each with the same fields
    - `preview_fallback: True` → file too large, opened in macOS Preview
    - Nested RV job payloads (`result`, `final_state`, storyboard scenes, atos)
    """
    if not isinstance(result, dict):
        return
    seen = _seen if _seen is not None else set()
    obj_id = id(result)
    if obj_id in seen:
        return
    seen.add(obj_id)
    try:
        if isinstance(result.get("chat_preview"), dict):
            media_extra["templatePreview"] = result["chat_preview"]  # type: ignore[assignment]
        if isinstance(result.get("mini_curriculum_preview"), dict):
            media_extra["miniCurriculumPreview"] = result["mini_curriculum_preview"]  # type: ignore[assignment]
        if isinstance(result.get("creative_widget"), dict):
            media_extra["creativeWidget"] = result["creative_widget"]  # type: ignore[assignment]
        if isinstance(result.get("roteiroLabOpen"), dict):
            media_extra["roteiroLabOpen"] = result["roteiroLabOpen"]  # type: ignore[assignment]
        if isinstance(result.get("bookEditorOpen"), dict):
            media_extra["bookEditorOpen"] = result["bookEditorOpen"]  # type: ignore[assignment]
        if isinstance(result.get("bookCoverEditorOpen"), dict):
            media_extra["bookCoverEditorOpen"] = result["bookCoverEditorOpen"]  # type: ignore[assignment]
        if isinstance(result.get("socialPostEditorOpen"), dict):
            media_extra["socialPostEditorOpen"] = result["socialPostEditorOpen"]  # type: ignore[assignment]
        if result.get("latexFiguresOpen"):
            media_extra["latexFiguresOpen"] = result["latexFiguresOpen"]  # type: ignore[assignment]

        # Preview fallback: file too large for inline, propagate to frontend
        if result.get("preview_fallback"):
            media_extra["preview_fallback"] = True  # type: ignore[assignment]
            if result.get("image_path"):
                media_extra["image_path"] = result["image_path"]  # type: ignore[assignment]
                dl = _image_download_url_from_path(str(result["image_path"]))
                if dl:
                    media_extra["download_url"] = dl  # type: ignore[assignment]
            if result.get("video_path"):
                media_extra["video_path"] = result["video_path"]  # type: ignore[assignment]
            if result.get("caption"):
                media_extra["caption"] = result["caption"]  # type: ignore[assignment]
            if result.get("message"):
                media_extra["message"] = result["message"]  # type: ignore[assignment]
            return

        pushed_image = False
        # Flat single image
        for fld in _MEDIA_IMAGE_FIELDS:
            src = _safe_media_url(result.get(fld))
            if src:
                _push_image(
                    media_extra,
                    src,
                    caption=_media_caption_from_item(result),
                    image_path=result.get("image_path") or result.get("path"),
                    download_url=result.get("download_url"),
                )
                pushed_image = True
                break
        if not pushed_image:
            _push_image_from_path_item(media_extra, result)
        # Flat single video
        for fld in _MEDIA_VIDEO_FIELDS:
            src = _safe_media_url(result.get(fld))
            if src:
                _push_video(
                    media_extra,
                    src,
                    poster=result.get("poster_data_url") or result.get("poster_url"),
                    caption=_media_caption_from_item(result),
                    video_path=result.get("video_path") or result.get("path"),
                    duration_s=result.get("duration_s") or result.get("duration"),
                )
                break
        # Arrays (storyboard scenes, atos, batches)
        for array_key in _SCENE_ARRAY_KEYS:
            for item in result.get(array_key) or []:
                if not isinstance(item, dict):
                    continue
                if array_key == "videos":
                    src = _safe_media_url(item.get("video_url") or item.get("video_data_url"))
                    if src:
                        _push_video(
                            media_extra,
                            src,
                            poster=item.get("poster_data_url") or item.get("poster_url"),
                            caption=_media_caption_from_item(item),
                            video_path=item.get("video_path") or item.get("path"),
                            duration_s=item.get("duration_s") or item.get("duration"),
                        )
                    continue
                src = _safe_media_url(
                    item.get("image_data_url")
                    or item.get("image_url")
                    or item.get("url")
                    or item.get("download_url")
                )
                if src:
                    _push_image(
                        media_extra,
                        src,
                        caption=_media_caption_from_item(item),
                        image_path=item.get("image_path") or item.get("path"),
                        download_url=item.get("download_url"),
                    )
                else:
                    _push_image_from_path_item(media_extra, item)
        # Nested RV payloads (skills_run, thumbnail/storybook job status)
        for nest_key in _NESTED_MEDIA_KEYS:
            nested = result.get(nest_key)
            if isinstance(nested, dict):
                _collect_media_from_tool_result(nested, media_extra, _seen=seen)
    except Exception:  # pragma: no cover — robustness
        log.debug("media collection failed", exc_info=True)


def _collect_attachments_from_tool_result(
    result: dict[str, Any],
    attachments_extra: list[dict[str, Any]],
) -> None:
    """Append downloadable file attachments (ZIP, PDF, etc.) from tool results."""
    try:
        items: list[dict[str, Any]] = []
        for item in result.get("attachments") or []:
            if isinstance(item, dict):
                items.append(item)
        single = result.get("attachment")
        if isinstance(single, dict):
            items.append(single)
        for email in result.get("emails") or []:
            if not isinstance(email, dict):
                continue
            for att in email.get("attachments") or []:
                if not isinstance(att, dict):
                    continue
                saved = str(att.get("saved_to") or att.get("path") or "").strip()
                if not saved:
                    continue
                items.append(
                    {
                        "name": str(att.get("filename") or att.get("name") or Path(saved).name),
                        "path": saved,
                        "size": att.get("size"),
                        "mime_type": att.get("mime_type"),
                        "download_url": att.get("download_url"),
                    }
                )

        for item in items:
            path = str(item.get("path") or item.get("saved_to") or "").strip()
            name = str(item.get("name") or item.get("filename") or "").strip()
            if not name and path:
                name = Path(path).name
            if not path or not name:
                continue
            entry = dict(item)
            if not entry.get("download_url"):
                top_url = str(result.get("download_url") or "").strip()
                if top_url:
                    entry["download_url"] = top_url
                else:
                    try:
                        from .chat_artifacts import artifact_download_url

                        entry["download_url"] = artifact_download_url(Path(path))
                    except Exception:
                        pass
            attachments_extra.append(entry)
    except Exception:  # pragma: no cover
        log.debug("attachment collection failed", exc_info=True)


_SHOW_MEDIA_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp"}
_SHOW_MEDIA_VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v", ".mkv"}
_SHOW_MEDIA_IMAGE_MAX = 8 * 1024 * 1024  # 8 MB
_SHOW_MEDIA_VIDEO_DATAURL_MAX = 4 * 1024 * 1024  # 4 MB
_HYDRATE_FILE_MAX_BYTES = _SHOW_MEDIA_IMAGE_MAX


def _show_media_kind_from_ext(ext: str) -> str:
    e = (ext or "").lower()
    if e in _SHOW_MEDIA_IMAGE_EXT:
        return "image"
    if e in _SHOW_MEDIA_VIDEO_EXT:
        return "video"
    return ""


def _show_media_to_data_url(path: Path, *, prefer_kind: str = "") -> tuple[str, str]:
    """Return (data_url, mime) for a local file."""
    from .image_file_utils import mime_type_for_path

    data = path.read_bytes()
    mime = mime_type_for_path(path, data=data[:16])
    if mime == "application/octet-stream":
        kind = prefer_kind or _show_media_kind_from_ext(path.suffix)
        if kind == "image":
            mime = "image/png"
        elif kind == "video":
            mime = "video/mp4"
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}", mime


def _run_show_media(
    *,
    path_or_url: str,
    kind: str = "auto",
    caption: str = "",
    poster: str = "",
) -> dict[str, Any]:
    """Implementation for the ``qclaw_show_media`` tool.

    Accepts a local absolute path or an https/http URL and produces the canonical
    media payload (image_data_url/image_url or video_url/video_data_url +
    poster_data_url) so the chat front-end renders it inline via the
    ``extras.media`` block.
    """
    src = (path_or_url or "").strip()
    if not src:
        return {"ok": False, "error": "path_or_url é obrigatório"}
    kind = (kind or "auto").lower()
    if kind not in {"auto", "image", "video"}:
        return {"ok": False, "error": f"kind inválido: {kind!r}"}

    is_url = src.startswith(("https://", "http://"))
    if not is_url:
        try:
            path = Path(src).expanduser().resolve()
        except Exception as e:
            return {"ok": False, "error": f"path inválido: {e}"}
        if not path.is_file():
            return {"ok": False, "error": f"arquivo não encontrado: {path}"}
        if kind == "auto":
            kind = _show_media_kind_from_ext(path.suffix) or "image"
        size = path.stat().st_size

        if kind == "image":
            if size > _SHOW_MEDIA_IMAGE_MAX:
                # Fallback: open in macOS Preview instead of inline
                try:
                    subprocess.Popen(["open", str(path)])
                except OSError:
                    pass
                return {
                    "ok": True,
                    "preview_fallback": True,
                    "image_path": str(path),
                    "caption": caption or "",
                    "message": (
                        f"Imagem com {size} bytes excede limite para exibição inline. "
                        "Aberta no Preview do macOS."
                    ),
                }
            data_url, _ = _show_media_to_data_url(path, prefer_kind="image")
            out: dict[str, Any] = {
                "ok": True,
                "image_data_url": data_url,
                "image_path": str(path),
            }
            if caption:
                out["caption"] = caption
            return out

        if kind == "video":
            if size > _SHOW_MEDIA_VIDEO_DATAURL_MAX:
                # Fallback: open in macOS Preview instead of inline
                try:
                    subprocess.Popen(["open", str(path)])
                except OSError:
                    pass
                return {
                    "ok": True,
                    "preview_fallback": True,
                    "video_path": str(path),
                    "caption": caption or "",
                    "message": (
                        f"Vídeo com {size} bytes excede limite para exibição inline. "
                        "Aberto no Preview do macOS."
                    ),
                }
            data_url, _ = _show_media_to_data_url(path, prefer_kind="video")
            out = {
                "ok": True,
                "video_data_url": data_url,
                "video_path": str(path),
            }
            if caption:
                out["caption"] = caption
            poster_url = _resolve_show_media_poster(poster)
            if poster_url:
                out["poster_data_url"] = poster_url
            return out

        return {"ok": False, "error": f"kind não suportado para arquivo local: {kind}"}

    # URL branch
    if kind == "auto":
        suffix = Path(src.split("?", 1)[0].split("#", 1)[0]).suffix
        kind = _show_media_kind_from_ext(suffix) or "image"
    if kind == "image":
        out = {"ok": True, "image_url": src}
        if caption:
            out["caption"] = caption
        return out
    if kind == "video":
        out = {"ok": True, "video_url": src}
        if caption:
            out["caption"] = caption
        poster_url = _resolve_show_media_poster(poster)
        if poster_url:
            out["poster_data_url"] = poster_url
        return out
    return {"ok": False, "error": f"kind não suportado: {kind}"}


def _resolve_show_media_poster(poster: str) -> str:
    """Resolve a poster argument (URL or local path) to a usable src."""
    p = (poster or "").strip()
    if not p:
        return ""
    if p.startswith(("https://", "http://", "data:image/")):
        return p
    try:
        path = Path(p).expanduser().resolve()
        if not path.is_file():
            return ""
        if path.stat().st_size > _SHOW_MEDIA_IMAGE_MAX:
            return ""
        data_url, _ = _show_media_to_data_url(path, prefer_kind="image")
        return data_url
    except Exception:
        return ""


def _run_show_slides_pdf(
    *,
    path: str,
    pages: str = "",
    max_pages: int = 12,
    dpi: int = 150,
    progress_job_id: Optional[str] = None,
) -> dict[str, Any]:
    """Render PDF/slide pages to PNG and return inline chat media payload."""
    from .chat_jobs import set_block_progress, update_job_media

    src = (path or "").strip()
    if not src:
        return {"ok": False, "error": "path é obrigatório"}

    try:
        input_path = Path(src).expanduser().resolve()
    except Exception as exc:
        return {"ok": False, "error": f"path inválido: {exc}"}

    if not input_path.is_file():
        return {"ok": False, "error": f"arquivo não encontrado: {input_path}"}

    max_pages = max(1, min(int(max_pages or 12), _MEDIA_TOTAL_LIMIT))
    dpi = max(72, min(int(dpi or 150), 300))
    page_range = (pages or "").strip() or None

    streaming_images: list[dict[str, Any]] = []

    def _on_page_progress(page_num: int, total: int, message: str) -> None:
        if not progress_job_id:
            return
        turn = max(0, int(page_num))
        block_total = max(1, int(total))
        set_block_progress(
            progress_job_id,
            turn,
            block_total,
            message=message or None,
        )

    from .chat_artifacts import resolve_previews_dir

    out_dir = resolve_previews_dir() / f"pdf-{int(time.time() * 1000)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        rendered, total_pages = render_document_pages(
            input_path,
            out_dir,
            dpi=dpi,
            pages=page_range,
            max_pages=max_pages,
            on_page_progress=_on_page_progress,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        return {"ok": False, "error": str(exc)}

    if not rendered:
        return {"ok": False, "error": "nenhuma página renderizada"}

    images: list[dict[str, Any]] = []
    encode_total = len(rendered)
    for idx, png_path in enumerate(rendered, start=1):
        page_num = page_number_from_path(png_path) or idx
        batch_total = max(1, encode_total)
        doc_total = int(total_pages) if total_pages else batch_total
        caption = (
            f"Página {page_num}/{doc_total}"
            if page_num is not None and doc_total > batch_total
            else (
                f"Página {idx}/{batch_total}"
                if page_num is not None
                else png_path.name
            )
        )
        if progress_job_id:
            from .chat_jobs import set_block_progress

            msg = f"A preparar página {idx}/{batch_total}…"
            if doc_total > batch_total:
                msg = (
                    f"A preparar página {page_num}/{batch_total} "
                    f"(documento: {doc_total} páginas)…"
                )
            set_block_progress(
                progress_job_id,
                idx,
                batch_total,
                message=msg,
            )
        size = png_path.stat().st_size
        dl_url = _image_download_url_from_path(str(png_path))
        if size > _SHOW_MEDIA_IMAGE_MAX:
            entry = {
                "page": page_num,
                "caption": caption,
                "image_path": str(png_path),
                "preview_fallback": True,
                "message": (
                    f"{caption}: {size} bytes excede limite inline "
                    "(reduza dpi ou intervalo)."
                ),
            }
            if dl_url:
                entry["download_url"] = dl_url
            images.append(entry)
            if progress_job_id:
                stream_entry: dict[str, Any] = {
                    "caption": caption,
                    "image_path": str(png_path.resolve()),
                    "preview_fallback": True,
                }
                if dl_url:
                    stream_entry["download_url"] = dl_url
                streaming_images.append(stream_entry)
                update_job_media(
                    progress_job_id,
                    {"images": list(streaming_images), "videos": []},
                )
            continue
        data_url, _ = _show_media_to_data_url(png_path, prefer_kind="image")
        entry = {
            "page": page_num,
            "caption": caption,
            "image_path": str(png_path),
            "image_data_url": data_url,
        }
        if dl_url:
            entry["download_url"] = dl_url
        images.append(entry)
        if progress_job_id:
            stream_entry = {
                "caption": caption,
                "image_path": str(png_path.resolve()),
            }
            if dl_url:
                stream_entry["download_url"] = dl_url
            streaming_images.append(stream_entry)
            update_job_media(
                progress_job_id,
                {"images": list(streaming_images), "videos": []},
            )

    inline = [item for item in images if item.get("image_data_url")]
    out: dict[str, Any] = {
        "ok": True,
        "source_path": str(input_path),
        "total_pages": total_pages,
        "shown_pages": len(rendered),
        "render_dir": str(out_dir),
        "images": images,
    }
    if inline:
        out["image_data_url"] = inline[0]["image_data_url"]
        out["image_path"] = inline[0]["image_path"]
        out["caption"] = inline[0]["caption"]
    if any(item.get("preview_fallback") for item in images):
        out["preview_fallback"] = True
        out["message"] = (
            "Algumas páginas excedem o limite inline; reduza dpi ou max_pages."
        )
    if total_pages and len(rendered) < int(total_pages):
        out["pages_remaining"] = int(total_pages) - len(rendered)
        out["message"] = (
            f"Renderizadas {len(rendered)} de {total_pages} páginas "
            f"(limite max_pages={max_pages}). "
            "Peça o intervalo seguinte com --pages ou aumente max_pages."
        )
    return out


def whatsapp_send_rendered_pdf(
    *,
    recipient: str,
    source_path: str,
    pages: str | None = None,
    caption: str = "",
    max_pages: int = 12,
) -> dict[str, Any]:
    """Send rendered PDF pages to WhatsApp (best-effort after preview job)."""
    recipient = str(recipient or "").strip()
    if not recipient:
        return {"ok": False, "error": "destinatário WhatsApp vazio"}
    try:
        import importlib.util

        repo_root = Path(__file__).resolve().parents[2]
        script = (
            repo_root
            / "skills/qclaw-whatsapp-media-preview/scripts/send_preview.py"
        )
        spec = importlib.util.spec_from_file_location("wa_send_preview", script)
        if spec is None or spec.loader is None:
            return {"ok": False, "error": f"script não encontrado: {script}"}
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.send_preview(
            recipient=recipient,
            input_path=Path(source_path),
            pages=pages,
            caption=caption,
            max_pages=max(1, min(int(max_pages or 12), 12)),
        )
    except Exception as exc:
        log.warning("whatsapp_send_rendered_pdf failed: %s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def spawn_slides_pdf_background(
    *,
    persistence: Any = None,
    session_key: str = "",
    **pdf_kwargs: Any,
) -> dict[str, Any]:
    """Render PDF/slides in a daemon thread with block progress."""
    from .chat_tool_background import spawn_chat_tool_background

    wa_to = str(pdf_kwargs.pop("whatsapp_to", "") or "").strip()
    wa_caption = str(pdf_kwargs.pop("whatsapp_caption", "") or "").strip()
    label = Path(str(pdf_kwargs.get("path") or "document")).stem
    max_pages = int(pdf_kwargs.get("max_pages") or 12)
    src_path = Path(str(pdf_kwargs.get("path") or "")).expanduser()

    def _prime(job_id: str) -> None:
        from .chat_jobs import set_block_progress

        doc_pages: int | None = None
        if src_path.is_file() and src_path.suffix.lower() == ".pdf":
            try:
                counted = pdf_page_count(src_path)
                if counted and counted > 0:
                    doc_pages = counted
            except Exception:
                pass
        batch_total = max(1, min(max_pages, doc_pages or max_pages))
        msg = f"A renderizar {label} ({batch_total} página(s)"
        if doc_pages and doc_pages > batch_total:
            msg += f" de {doc_pages} no documento"
        msg += ")…"
        set_block_progress(job_id, 0, batch_total, message=msg)

    def _run(job_id: str) -> dict[str, Any]:
        result = _run_show_slides_pdf(progress_job_id=job_id, **pdf_kwargs)
        if result.get("ok") and wa_to:
            from .chat_jobs import append_progress

            append_progress(job_id, f"A enviar preview no WhatsApp para {wa_to}…")
            send_out = whatsapp_send_rendered_pdf(
                recipient=wa_to,
                source_path=str(result.get("source_path") or pdf_kwargs.get("path") or ""),
                pages=str(pdf_kwargs.get("pages") or "").strip() or None,
                caption=wa_caption or label,
                max_pages=max_pages,
            )
            result["whatsapp_send"] = send_out
            if not send_out.get("ok"):
                result["ok"] = False
                result["error"] = str(send_out.get("error") or "falha ao enviar WhatsApp")
        return result

    def _format_success(result: dict[str, Any]) -> str:
        shown = int(result.get("shown_pages") or 0)
        total = result.get("total_pages")
        head = f"PDF **{label}** renderizado — **{shown}** página(s)"
        if total is not None:
            head += f" de **{total}**"
        head += "."
        wa = result.get("whatsapp_send") if isinstance(result.get("whatsapp_send"), dict) else None
        if wa and wa.get("ok"):
            head += (
                f" Enviado no WhatsApp (**{wa.get('sent_count', 0)}** imagem(ns) "
                f"para `{wa_to}`)."
            )
        elif wa:
            head += f" ⚠️ Falha no envio WhatsApp: {str(wa.get('error') or '')[:200]}"
        remaining = result.get("pages_remaining")
        if remaining:
            head += f" Faltam **{remaining}** páginas — peça o lote seguinte."
        return head

    return spawn_chat_tool_background(
        kind="pdf_preview",
        session_key=session_key,
        message_text=f"[pdf_preview] {label}" + (f" → {wa_to}" if wa_to else ""),
        label=f"Render PDF em background — {label}",
        run_fn=_run,
        persistence=persistence,
        on_start=_prime,
        format_success=_format_success,
    )

