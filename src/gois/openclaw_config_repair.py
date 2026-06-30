"""Repair OpenClaw/QClaw gateway configs that block startup with unknown channels."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.request import urlopen

from .local_paths import openclaw_bundle_base, openclaw_state_dir

_DEFAULT_QCLAW_STATE = Path.home() / ".qclaw-oversea"


def resolve_qclaw_gateway_state_dir(
    explicit: Optional[Path] = None,
) -> Path:
    """Pick the writable OpenClaw state dir used by the QClaw desktop app."""
    if explicit is not None:
        return explicit.expanduser()
    for key in ("OPENCLAW_STATE_DIR", "GOIS_OPENCLAW_STATE_DIR"):
        raw = os.environ.get(key)
        if raw:
            return Path(raw).expanduser()
    if _DEFAULT_QCLAW_STATE.is_dir():
        return _DEFAULT_QCLAW_STATE
    return openclaw_state_dir()


# Channels that crash or stall the bundled openclaw-gateway on this install.
_BLOCKED_CHANNELS = frozenset(
    {
        "wechat-access",
        "spectrum",
        "qqbot",
        "yuanbao",
        "dingtalk-connector",
        "wecom",
        "openclaw-weixin",
        "feishu",
        "discord",
        "slack",
    }
)

_BLOCKED_PLUGIN_ENTRIES = frozenset(
    {
        "wechat-access",
        "skill-interceptor",
        "openclaw-weixin",
        "openclaw-qqbot",
        "openclaw-plugin-yuanbao",
    }
)

_BLOCKED_PLUGIN_ALLOW = frozenset(
    {
        "wechat-access",
        "openclaw-weixin",
        "openclaw-qqbot",
    }
)

_DEFAULT_TELEGRAM_CHANNEL: dict[str, Any] = {
    "enabled": True,
    "allowFrom": ["*"],
    "dmPolicy": "open",
    "botToken": "${QCLAW_TELEGRAM_BOT_TOKEN}",
}

# QClaw template ships ``qclaw/modelroute`` but the proxy only accepts V4 ids.
_INVALID_QCLAW_MODEL_IDS = frozenset({"modelroute"})
_DEFAULT_QCLAW_MODEL_ID = "deepseek-v4-pro"
_SUPPORTED_QCLAW_MODELS: tuple[dict[str, Any], ...] = (
    {
        "id": "deepseek-v4-pro",
        "name": "DeepSeek V4 Pro",
        "input": ["text", "image"],
    },
    {
        "id": "deepseek-v4-flash",
        "name": "DeepSeek V4 Flash",
        "input": ["text", "image"],
    },
)


def _is_invalid_qclaw_model_ref(value: str) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return False
    if raw in _INVALID_QCLAW_MODEL_IDS:
        return True
    if "/" in raw:
        return raw.split("/", 1)[1] in _INVALID_QCLAW_MODEL_IDS
    return False


def parse_openclaw_version(raw: str) -> tuple[int, ...]:
    """Parse OpenClaw semver-ish strings like ``2026.4.21`` for comparisons."""
    parts: list[int] = []
    for chunk in re.split(r"[.\-+]", str(raw or "").strip()):
        if chunk.isdigit():
            parts.append(int(chunk))
    return tuple(parts) if parts else (0,)


def openclaw_supports_bundled_discovery(version: tuple[int, ...]) -> bool:
    return version >= (2026, 6)


def _read_openclaw_version_from_package(package_json: Path) -> Optional[tuple[int, ...]]:
    if not package_json.is_file():
        return None
    try:
        data = json.loads(package_json.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    version = parse_openclaw_version(str(data.get("version") or ""))
    return version if version != (0,) else None


def detect_qclaw_openclaw_version(
    *,
    state_dir: Optional[Path] = None,
) -> Optional[tuple[int, ...]]:
    """Best-effort OpenClaw version bundled with the QClaw desktop app."""
    root = state_dir or resolve_qclaw_gateway_state_dir()
    candidates: list[Path] = [
        qclaw_template_openclaw_json().parent.parent / "node_modules" / "openclaw" / "package.json",
    ]
    qclaw_meta = root / "qclaw.json"
    if qclaw_meta.is_file():
        try:
            meta = json.loads(qclaw_meta.read_text())
            mjs = str((meta.get("cli") or {}).get("openclawMjs") or "").strip()
            if mjs:
                candidates.insert(0, Path(mjs).expanduser().parent / "package.json")
        except (OSError, json.JSONDecodeError):
            pass
    for path in candidates:
        version = _read_openclaw_version_from_package(path)
        if version is not None:
            return version
    return None


def analyze_legacy_openclaw_config(
    config: dict[str, Any],
    *,
    openclaw_version: Optional[tuple[int, ...]] = None,
) -> list[str]:
    """Return schema issues for the target OpenClaw version."""
    version = openclaw_version if openclaw_version is not None else detect_qclaw_openclaw_version()
    supports_bundled = (
        openclaw_supports_bundled_discovery(version)
        if version is not None
        else False
    )
    issues: list[str] = []
    defaults = (config.get("agents") or {}).get("defaults")
    if supports_bundled and isinstance(defaults, dict) and defaults.get("llm") is not None:
        issues.append(
            "agents.defaults.llm is legacy; remove it and use "
            "models.providers.<id>.timeoutSeconds"
        )

    plugins = config.get("plugins")
    if isinstance(plugins, dict):
        allow = plugins.get("allow")
        bundled = plugins.get("bundledDiscovery")
        if supports_bundled and isinstance(allow, list) and allow and bundled is None:
            issues.append(
                'plugins.allow without plugins.bundledDiscovery; set bundledDiscovery="compat"'
            )
        if not supports_bundled and bundled is not None:
            issues.append(
                "plugins.bundledDiscovery is unsupported on OpenClaw <2026.6; remove it"
            )

    session = config.get("session")
    if supports_bundled and isinstance(session, dict):
        maintenance = session.get("maintenance")
        if isinstance(maintenance, dict) and maintenance.get("rotateBytes") is not None:
            issues.append("session.maintenance.rotateBytes is deprecated; remove it")

    return issues


def fix_legacy_openclaw_config_keys(
    config: dict[str, Any],
    *,
    openclaw_version: Optional[tuple[int, ...]] = None,
) -> dict[str, Any]:
    """Normalize schema keys for the bundled OpenClaw version."""
    version = openclaw_version if openclaw_version is not None else detect_qclaw_openclaw_version()
    supports_bundled = (
        openclaw_supports_bundled_discovery(version)
        if version is not None
        else False
    )
    out = dict(config)

    if supports_bundled:
        agents = dict(out.get("agents") or {})
        defaults = dict(agents.get("defaults") or {})
        if defaults.pop("llm", None) is not None:
            agents["defaults"] = defaults
            out["agents"] = agents

        session = dict(out.get("session") or {})
        maintenance = dict(session.get("maintenance") or {})
        if maintenance.pop("rotateBytes", None) is not None:
            session["maintenance"] = maintenance
            out["session"] = session

    plugins = dict(out.get("plugins") or {})
    allow = plugins.get("allow")
    if supports_bundled:
        if isinstance(allow, list) and allow and plugins.get("bundledDiscovery") is None:
            plugins["bundledDiscovery"] = "compat"
            out["plugins"] = plugins
    elif plugins.pop("bundledDiscovery", None) is not None:
        if plugins:
            out["plugins"] = plugins
        elif "plugins" in out:
            out.pop("plugins")

    return out


def fix_broken_qclaw_models(config: dict[str, Any]) -> dict[str, Any]:
    """Replace invalid ``modelroute`` refs with supported DeepSeek V4 model ids."""
    out = dict(config)

    agents = dict(out.get("agents") or {})
    defaults = dict(agents.get("defaults") or {})
    model_cfg = dict(defaults.get("model") or {})
    primary = str(model_cfg.get("primary") or "")
    if _is_invalid_qclaw_model_ref(primary):
        provider = primary.split("/", 1)[0] if "/" in primary else "qclaw"
        model_cfg["primary"] = f"{provider}/{_DEFAULT_QCLAW_MODEL_ID}"
        defaults["model"] = model_cfg
        agents["defaults"] = defaults
        out["agents"] = agents

    models = dict(out.get("models") or {})
    providers = dict(models.get("providers") or {})
    qclaw = dict(providers.get("qclaw") or {})
    model_list = list(qclaw.get("models") or [])
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in model_list:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip().lower()
        if mid in _INVALID_QCLAW_MODEL_IDS:
            continue
        kept.append(dict(item))
        seen.add(mid)

    for entry in _SUPPORTED_QCLAW_MODELS:
        mid = str(entry["id"])
        if mid not in seen:
            kept.append(dict(entry))
            seen.add(mid)

    if not kept:
        kept = [dict(entry) for entry in _SUPPORTED_QCLAW_MODELS]

    qclaw["models"] = kept
    providers["qclaw"] = qclaw
    models["providers"] = providers
    out["models"] = models
    return out


def strip_broken_openclaw_channels(config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``config`` with broken channels/plugins removed."""
    out = dict(config)
    channels = dict(out.get("channels") or {})
    for name in list(channels.keys()):
        if name in _BLOCKED_CHANNELS:
            channels.pop(name, None)
    if "telegram" not in channels:
        channels["telegram"] = dict(_DEFAULT_TELEGRAM_CHANNEL)
    out["channels"] = channels

    plugins = dict(out.get("plugins") or {})
    entries = dict(plugins.get("entries") or {})
    for name in list(entries.keys()):
        if name in _BLOCKED_PLUGIN_ENTRIES:
            entries.pop(name, None)
    if entries:
        plugins["entries"] = entries
    elif "entries" in plugins:
        plugins.pop("entries")

    allow = plugins.get("allow")
    if isinstance(allow, list):
        filtered = [item for item in allow if item not in _BLOCKED_PLUGIN_ALLOW]
        if filtered:
            plugins["allow"] = filtered
        else:
            plugins.pop("allow", None)

    if plugins:
        out["plugins"] = plugins
    elif "plugins" in out:
        out.pop("plugins")

    return out


def qclaw_template_openclaw_json() -> Path:
    """Bundled QClaw template that re-poisons user state on app startup."""
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "QClaw"
        / "openclaw"
        / "config"
        / "openclaw.json"
    )


def default_openclaw_config_paths(
    *,
    state_dir: Optional[Path] = None,
    include_template: bool = True,
) -> list[Path]:
    """Paths that should be scrubbed before gateway start or doctor --fix."""
    root = state_dir or resolve_qclaw_gateway_state_dir()
    paths = [
        root / "openclaw.json",
        root / "openclaw.json.bak",
        root / "openclaw.json.last-good",
    ]
    if include_template:
        paths.append(qclaw_template_openclaw_json())
    return paths


@dataclass
class ConfigRepairResult:
    path: Path
    changed: bool
    channels_before: list[str]
    channels_after: list[str]
    error: Optional[str] = None


def repair_openclaw_config_files(
    paths: list[Path],
    *,
    dry_run: bool = False,
) -> list[ConfigRepairResult]:
    """Strip broken channels from each existing JSON file."""
    results: list[ConfigRepairResult] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            results.append(
                ConfigRepairResult(
                    path=path,
                    changed=False,
                    channels_before=[],
                    channels_after=[],
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        before = list((raw.get("channels") or {}).keys())
        openclaw_version = detect_qclaw_openclaw_version()
        cleaned = fix_legacy_openclaw_config_keys(
            fix_broken_qclaw_models(strip_broken_openclaw_channels(raw)),
            openclaw_version=openclaw_version,
        )
        after = list((cleaned.get("channels") or {}).keys())
        changed = cleaned != raw
        if changed and not dry_run:
            path.write_text(json.dumps(cleaned, indent=2) + "\n")
        results.append(
            ConfigRepairResult(
                path=path,
                changed=changed,
                channels_before=before,
                channels_after=after,
            )
        )
    return results


def _resolve_openclaw_cli(
    base: Optional[Path] = None,
) -> tuple[Optional[Path], Optional[Path], Optional[Path]]:
    bundle = base or openclaw_bundle_base()
    bin_path = bundle / "config" / "bin" / "openclaw"
    node_path = bundle / "config" / "bin" / "node"
    mjs_path = bundle / "node_modules" / "openclaw" / "openclaw.mjs"
    app_node = Path(
        "/Applications/QClaw.app/Contents/Resources/node/node"
    )
    if not node_path.is_file() and app_node.is_file():
        node_path = app_node
    return (
        bin_path if bin_path.is_file() else None,
        node_path if node_path.is_file() else None,
        mjs_path if mjs_path.is_file() else None,
    )


def _openclaw_env(state_dir: Path) -> dict[str, str]:
    config_path = state_dir / "openclaw.json"
    env = dict(os.environ)
    env["OPENCLAW_CONFIG_PATH"] = str(config_path)
    env["OPENCLAW_STATE_DIR"] = str(state_dir)
    return env


def restart_openclaw_gateway(
    *,
    state_dir: Optional[Path] = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Stop and start the LaunchAgent gateway via the openclaw CLI."""
    root = state_dir or resolve_qclaw_gateway_state_dir()
    bin_path, node_path, mjs_path = _resolve_openclaw_cli()
    if not bin_path or not node_path or not mjs_path:
        return {
            "ok": False,
            "reason": "openclaw CLI not found (install QClaw or set GOIS_OPENCLAW_BASE)",
        }

    env = _openclaw_env(root)
    env["QCLAW_CLI_NODE_BINARY"] = str(node_path)
    env["QCLAW_CLI_OPENCLAW_MJS"] = str(mjs_path)
    outputs: list[str] = []
    for action in ("stop", "start"):
        proc = subprocess.run(
            [str(bin_path), "gateway", action],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()
        if tail:
            outputs.append(f"{action}: {tail[-1]}")
        if proc.returncode != 0 and action == "start":
            return {
                "ok": False,
                "rc": proc.returncode,
                "reason": f"gateway {action} failed",
                "output": "\n".join(outputs),
            }
    return {"ok": True, "output": "\n".join(outputs)}


def probe_gateway_http(
    port: int,
    *,
    host: str = "127.0.0.1",
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    url = f"http://{host}:{port}/"
    try:
        with urlopen(url, timeout=timeout_seconds) as resp:
            return {"ok": resp.status == 200, "url": url, "status": resp.status}
    except Exception as exc:
        return {
            "ok": False,
            "url": url,
            "error": f"{type(exc).__name__}: {exc}",
        }


def wait_for_gateway_http(
    port: int,
    *,
    timeout_seconds: float = 180.0,
    poll_interval_seconds: float = 5.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {"ok": False, "url": f"http://127.0.0.1:{port}/"}
    while time.monotonic() < deadline:
        last = probe_gateway_http(port)
        if last.get("ok"):
            return last
        time.sleep(poll_interval_seconds)
    return last


def read_gateway_port_file(
    state_dir: Optional[Path] = None,
) -> Optional[int]:
    root = state_dir or resolve_qclaw_gateway_state_dir()
    port_file = root / "qclaw.json"
    if not port_file.is_file():
        return None
    try:
        data = json.loads(port_file.read_text())
        raw = data.get("port")
        return int(raw) if raw is not None else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def repair_qclaw_gateway(
    *,
    state_dir: Optional[Path] = None,
    include_template: bool = True,
    restart_gateway: bool = True,
    wait_for_http: bool = True,
    dry_run: bool = False,
    wait_timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    """Full repair: scrub configs, optionally restart gateway, poll HTTP health."""
    root = state_dir or resolve_qclaw_gateway_state_dir()
    paths = default_openclaw_config_paths(
        state_dir=root,
        include_template=include_template,
    )
    repairs = repair_openclaw_config_files(paths, dry_run=dry_run)
    changed = [r for r in repairs if r.changed]
    out: dict[str, Any] = {
        "ok": True,
        "state_dir": str(root),
        "dry_run": dry_run,
        "files": [
            {
                "path": str(r.path),
                "changed": r.changed,
                "channels_before": r.channels_before,
                "channels_after": r.channels_after,
                "error": r.error,
            }
            for r in repairs
        ],
        "changed_count": len(changed),
    }

    if dry_run:
        out["message"] = "dry run — no files written, gateway not restarted"
        return out

    if restart_gateway:
        restart = restart_openclaw_gateway(state_dir=root)
        out["gateway_restart"] = restart
        if not restart.get("ok"):
            out["ok"] = False
            out["message"] = restart.get("reason") or "gateway restart failed"
            return out

    if wait_for_http:
        port = read_gateway_port_file(root)
        if port is None:
            out["ok"] = False
            out["message"] = f"no port in {root / 'qclaw.json'}"
            return out
        health = wait_for_gateway_http(port, timeout_seconds=wait_timeout_seconds)
        out["health"] = health
        if not health.get("ok"):
            out["ok"] = False
            out["message"] = health.get("error") or "gateway HTTP probe failed"
            return out

    out["message"] = "gateway configs repaired (channels, models, legacy keys)"
    return out
