"""Route RV API paths to local Python handlers (no external HTTP)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .config import use_local_rv
from .images import handle_generate_image, read_local_file
from .social import handle_social_concepts
from .styles import (
    analyze_face_bytes,
    analyze_face_path,
    analyze_image_bytes,
    analyze_image_path,
)
from .thumbnails import (
    handle_generate_thumbnail,
    handle_job_status,
    handle_thumbnail_prompt,
)

log = logging.getLogger(__name__)

# Fast paths — no need to boot full FastAPI app
_FAST_POST = frozenset(
    {
        "/tools/thumbnail/prompt",
        "/generate_thumbnail",
        "/generate_thumbnail_background",
        "/tools/generate-image",
        "/thumbnail",
        "/styles/analyze-face",
        "/styles/analyze-image",
        "/acts/update",
        "/acts/add",
        "/acts/remove",
        "/acts/plan",
        "/tools/init_manual_script",
        "/tools/init_manual_script_4acts",
    }
)


def _route_path(path: str) -> str:
    parsed = urlparse(path if "://" in path else f"http://local{path or '/'}")
    return parsed.path.rstrip("/") or "/"


def is_local_path(path: str) -> bool:
    if not use_local_rv():
        return False
    route = _route_path(path)
    if route in _FAST_POST:
        return True
    if re.match(r"^/acts/[^/]+/bundle$", route):
        return True
    if re.match(r"^/acts/[^/]+/\d+$", route):
        return True
    if re.match(r"^/acts/[^/]+/combine$", route):
        return True
    if route in ("/acts/update", "/acts/add", "/acts/remove", "/acts/plan"):
        return True
    if route in ("/tools/init_manual_script", "/tools/init_manual_script_4acts"):
        return True
    if re.match(r"^/status/[^/]+$", route):
        return True
    if re.match(r"^/jobs/[^/]+$", route):
        return True
    if route == "/files":
        return True
    try:
        from .embedded_api import embedded_available

        return embedded_available()
    except Exception:
        return False


def _try_fast_handler(
    method: str,
    route: str,
    *,
    body: dict[str, Any],
    qs: dict[str, str],
    file_bytes: Optional[bytes],
    file_path: Optional[str],
) -> Optional[dict[str, Any]]:
    m = method.upper()

    if m == "GET":
        status_match = re.match(r"^/status/([^/]+)$", route)
        jobs_match = re.match(r"^/jobs/([^/]+)$", route)
        acts_bundle_match = re.match(r"^/acts/([^/]+)/bundle$", route)
        acts_get_match = re.match(r"^/acts/([^/]+)/(\d+)$", route)
        if status_match or jobs_match:
            job_id = (status_match or jobs_match).group(1)
            try:
                return handle_job_status(job_id)
            except KeyError:
                return None
        if acts_bundle_match:
            from . import acts as local_acts

            return local_acts.get_bundle(acts_bundle_match.group(1))
        if acts_get_match:
            from . import acts as local_acts

            return local_acts.get_act(acts_get_match.group(1), int(acts_get_match.group(2)))
        if route == "/files":
            file_param = qs.get("path") or ""
            _, content = read_local_file(file_param)
            return {"_binary": content, "_file_path": file_param}
        return None

    if m != "POST":
        return None

    if route == "/tools/thumbnail/prompt":
        return handle_thumbnail_prompt(body)
    if route in ("/generate_thumbnail", "/generate_thumbnail_background"):
        return handle_generate_thumbnail(body)
    if route == "/tools/generate-image":
        return handle_generate_image(body)
    if route == "/thumbnail":
        return handle_social_concepts(body)
    if route == "/styles/analyze-image":
        api_key = str(body.get("api_key") or "").strip() or None
        if file_bytes is not None:
            return analyze_image_bytes(file_bytes, api_key=api_key)
        if file_path:
            return analyze_image_path(Path(file_path), api_key=api_key)
        raise ValueError("file required for /styles/analyze-image")
    if route == "/styles/analyze-face":
        api_key = str(body.get("api_key") or "").strip() or None
        if file_bytes is not None:
            return analyze_face_bytes(file_bytes, api_key=api_key)
        if file_path:
            return analyze_face_path(Path(file_path), api_key=api_key)
        raise ValueError("file required for /styles/analyze-face")
    if route == "/acts/update":
        from . import acts as local_acts

        local_acts.update_act(str(body["job_id"]), int(body["act_number"]), str(body["content"]))
        return {"success": True}
    if route == "/acts/add":
        from . import acts as local_acts

        return local_acts.add_act(
            str(body["job_id"]),
            position=body.get("position"),
            content=str(body.get("content") or ""),
            objective=str(body.get("objective") or ""),
            title=str(body.get("title") or ""),
        )
    if route == "/acts/remove":
        from . import acts as local_acts

        return local_acts.remove_act(str(body["job_id"]), int(body["act_number"]))
    if route == "/acts/plan":
        from . import acts as local_acts

        return local_acts.plan_acts(
            str(body["job_id"]),
            num_acts=body.get("num_acts"),
            api_key=body.get("api_key"),
        )
    if route == "/tools/init_manual_script_4acts":
        from . import acts as local_acts

        return local_acts.init_blank(
            topic=str(body.get("topic") or "Roteiro em branco"),
            num_acts=int(body.get("num_acts") or 4),
        )
    if route == "/tools/init_manual_script":
        from . import acts as local_acts

        return local_acts.init_manual(
            topic=str(body.get("topic") or "Roteiro manual"),
            script=str(body.get("script") or ""),
            author_styles=body.get("author_styles"),
        )
    acts_combine_match = re.match(r"^/acts/([^/]+)/combine$", route)
    if acts_combine_match:
        from . import acts as local_acts

        script = local_acts.combine(acts_combine_match.group(1))
        return {"success": True, "file": script}
    return None


def dispatch_local(
    method: str,
    path: str,
    *,
    payload: Optional[dict[str, Any]] = None,
    query: Optional[dict[str, str]] = None,
    file_bytes: Optional[bytes] = None,
    file_path: Optional[str] = None,
    file_name: str = "upload.bin",
    file_mime: str = "application/octet-stream",
    timeout: float = 120.0,
) -> Optional[dict[str, Any]]:
    if not use_local_rv():
        return None

    raw_path = path or "/"
    parsed = urlparse(raw_path if "://" in raw_path else f"http://local{raw_path}")
    route = parsed.path.rstrip("/") or "/"
    qs = query or {k: v[0] for k, v in parse_qs(parsed.query).items()}
    body = payload or {}

    try:
        fast = _try_fast_handler(
            method,
            route,
            body=body,
            qs=qs,
            file_bytes=file_bytes,
            file_path=file_path,
        )
        if fast is not None:
            return fast
    except KeyError:
        pass

    from .embedded_api import embedded_available, request_embedded

    if not embedded_available():
        return None

    log.debug("Embedded RV %s %s", method.upper(), route)
    return request_embedded(
        method,
        path,
        payload=body if method.upper() != "GET" else None,
        file_bytes=file_bytes,
        file_name=file_name,
        file_mime=file_mime,
        timeout=timeout,
    )


def local_mode_label() -> str:
    if not use_local_rv():
        return "remote"
    try:
        from .embedded_api import client_mode, embedded_available

        if embedded_available():
            mode = client_mode()
            if mode:
                return f"local+{mode}"
            return "local+embedded"
    except Exception:
        pass
    return "local"
