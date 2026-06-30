"""DeepSeek (OpenAI-compatible) recovery agent.

The agent receives a failure summary, then loops:
  1. Ask the model for the next step (with the tool catalogue attached).
  2. If the model returns `tool_calls`, execute them locally against the
     Recovery instance and append the JSON results back into the conversation.
  3. Stop when the model returns plain content (final report) or we hit
     `max_tool_iterations`.

DeepSeek serves an OpenAI-compatible Chat Completions API, so we use the
standard `openai` SDK pointed at `https://api.deepseek.com`. To swap in
another provider just change `agent.base_url` / `agent.model` /
`agent.api_key_env` in config.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, Awaitable, Optional

from .config import AgentConfig, OpenclawDoctorConfig
from .hermes_cron_diagnostic_agent import HermesCronDiagnosticExtras
from .hermes_recovery_agent import HermesRecoveryExtras, default_hermes_recovery_system_prompt
from .recovery import Recovery

log = logging.getLogger(__name__)


def _hermes_recovery_tool_specs(extras: HermesRecoveryExtras) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "hermes_dashboard_status",
                "description": (
                    "Check whether the Hermes web dashboard accepts HTTP "
                    "(same probe gois uses)."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "restart_hermes_dashboard",
                "description": (
                    "Stop stale `hermes dashboard` processes and spawn a fresh "
                    "dashboard using the monitor's configured start command."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "hermes_cron_snapshot",
                "description": (
                    "Summarize Hermes cron jobs: counts, jobs with last_status=error, "
                    "and jobs currently running."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "hermes_cron_retry",
                "description": (
                    "Re-run a failed Hermes cron job. Pass either job_name (from logs) "
                    "or log_line containing 'Job \\'name\\' failed'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_name": {"type": "string"},
                        "log_line": {"type": "string"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ensure_hermes_cron_gateway",
                "description": (
                    "Probe `hermes cron status` for the jobs_path HERMES_HOME and start "
                    "the gateway there if cron jobs would not fire."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "hermes_cron_scheduler_status",
                "description": (
                    "Read-only probe: is the Hermes gateway running for the cron jobs file "
                    "configured in gois (jobs_path / HERMES_HOME)?"
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    ]


def _hermes_recovery_dispatch(
    recovery: Recovery, extras: HermesRecoveryExtras
) -> dict[str, Callable[[dict], Awaitable[Any]]]:
    async def _dashboard_status(_args: dict) -> Any:
        up = await recovery.hermes_dashboard_up()
        return {"dashboard_up": up, "dashboard_url": recovery.cfg.dashboard_url}

    async def _restart_dashboard(_args: dict) -> str:
        parts = [await recovery.stop_hermes_dashboard()]
        parts.append(
            await recovery.start_hermes_dashboard(extras.dashboard_start_command)
        )
        up = await recovery.wait_hermes_dashboard_up(timeout_seconds=30.0)
        parts.append(f"dashboard_up_after_spawn={up}")
        return "\n".join(parts)

    async def _cron_snapshot(_args: dict) -> Any:
        if extras.cron_snapshot is None:
            return {"ok": False, "reason": "cron snapshot not configured"}
        return extras.cron_snapshot()

    async def _cron_retry(args: dict) -> Any:
        log_line = str(args.get("log_line") or "").strip()
        job_name = str(args.get("job_name") or "").strip()
        if not log_line and job_name:
            log_line = f"cron.scheduler: Job '{job_name}' failed"
        if not log_line:
            return {"ok": False, "reason": "provide job_name or log_line"}
        return await recovery.hermes_cron_retry(extras.cron_cfg, log_line)

    async def _cron_scheduler_status(_args: dict) -> Any:
        from pathlib import Path

        from .hermes_cron import probe_hermes_cron_scheduler

        jobs_path = Path(extras.cron_cfg.jobs_path).expanduser()
        return await asyncio.to_thread(probe_hermes_cron_scheduler, jobs_path)

    async def _ensure_cron_gateway(_args: dict) -> Any:
        return await recovery.ensure_hermes_cron_gateway(extras.cron_cfg)

    return {
        "hermes_dashboard_status": _dashboard_status,
        "restart_hermes_dashboard": _restart_dashboard,
        "hermes_cron_snapshot": _cron_snapshot,
        "hermes_cron_retry": _cron_retry,
        "hermes_cron_scheduler_status": _cron_scheduler_status,
        "ensure_hermes_cron_gateway": _ensure_cron_gateway,
    }


def _hermes_cron_diagnostic_tool_specs() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "hermes_cron_diagnostic_report",
                "description": (
                    "Structured read-only report: scheduler probe, HERMES_HOME mismatch, "
                    "jobs with errors, recent log failures, recommendations."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "Optional cron job id for deep dive",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "hermes_cron_job_result",
                "description": (
                    "Load the latest saved cron run output (markdown) for one job_id."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                    },
                    "required": ["job_id"],
                },
            },
        },
    ]


def _hermes_cron_diagnostic_dispatch(
    recovery: Recovery, extras: HermesCronDiagnosticExtras
) -> dict[str, Callable[[dict], Awaitable[Any]]]:
    async def _report(args: dict) -> Any:
        job_id = str(args.get("job_id") or "").strip() or None
        return extras.diagnostic_report(job_id)

    async def _snap(_args: dict) -> Any:
        return extras.cron_snapshot()

    async def _job_result(args: dict) -> Any:
        job_id = str(args.get("job_id") or "").strip()
        if not job_id:
            return {"ok": False, "error": "job_id is required"}
        return extras.cron_job_result(job_id)

    async def _cron_scheduler_status(_args: dict) -> Any:
        from pathlib import Path

        from .hermes_cron import probe_hermes_cron_scheduler

        jobs_path = Path(extras.cron_cfg.jobs_path).expanduser()
        return await asyncio.to_thread(probe_hermes_cron_scheduler, jobs_path)

    return {
        "hermes_cron_diagnostic_report": _report,
        "hermes_cron_job_result": _job_result,
        "hermes_cron_scheduler_status": _cron_scheduler_status,
        "hermes_cron_snapshot": _snap,
    }


def _tool_catalogue(
    recovery: Recovery,
    doctor_cfg: OpenclawDoctorConfig,
    *,
    hermes_extras: Optional[HermesRecoveryExtras] = None,
    cron_diagnostic_extras: Optional[HermesCronDiagnosticExtras] = None,
) -> list[dict]:
    """OpenAI-style function specs the model can call."""
    allowed_logs = recovery.cfg.log_paths
    restart_name = f"restart_{recovery.cfg.name}"
    tools = [
        {
            "type": "function",
            "function": {
                "name": "health_check",
                "description": (
                    "Probe every configured health signal (process via pgrep, "
                    "optional HTTP) and return per-check status."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "process_status",
                "description": (
                    "Broad pgrep against the service name; returns every "
                    "related process on the host (helpers, children, …)."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_log_tail",
                "description": (
                    "Read the last N lines of an allowed log file. `path` "
                    f"must be one of: {allowed_logs or '(no log_paths configured)'}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "lines": {"type": "integer", "default": 200},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": restart_name,
                "description": (
                    f"Stop (if a stop_command is configured) and start the "
                    f"{recovery.cfg.name} service. Use only when diagnosis "
                    "indicates restart is needed."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    ]
    # restart_name is already "restart_qclaw" for qclaw — do not register twice.
    if recovery.cfg.name != "qclaw" and restart_name != "restart_qclaw":
        tools.append({
            "type": "function",
            "function": {
                "name": "restart_qclaw",
                "description": f"Alias for {restart_name}.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        })
    if doctor_cfg.enabled:
        tools.append({
            "type": "function",
            "function": {
                "name": "openclaw_doctor_fix",
                "description": (
                    "Run `openclaw doctor --fix` to repair the openclaw config "
                    "(~/.openclaw/openclaw.json). Non-destructive — prefer this "
                    "BEFORE restart_qclaw for symptoms like 'Service connection "
                    "error', 'Gateway failed to start', or sync daemon crashes. "
                    f"Hard timeout {doctor_cfg.timeout_seconds:.0f}s."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        })
    seen_names: set[str] = {t["function"]["name"] for t in tools}
    if cron_diagnostic_extras is not None and recovery.cfg.name == "hermes":
        mini = HermesRecoveryExtras(
            cron_cfg=cron_diagnostic_extras.cron_cfg,
            dashboard_start_command=[],
            cron_snapshot=cron_diagnostic_extras.cron_snapshot,
        )
        for spec in _hermes_recovery_tool_specs(mini):
            name = spec["function"]["name"]
            if name in {"hermes_cron_scheduler_status", "hermes_cron_snapshot"} and name not in seen_names:
                tools.append(spec)
                seen_names.add(name)
        for spec in _hermes_cron_diagnostic_tool_specs():
            name = spec["function"]["name"]
            if name not in seen_names:
                tools.append(spec)
                seen_names.add(name)
    if hermes_extras is not None and recovery.cfg.name == "hermes":
        for spec in _hermes_recovery_tool_specs(hermes_extras):
            name = spec["function"]["name"]
            if name not in seen_names:
                tools.append(spec)
                seen_names.add(name)
    from .skills_mcp import chat_tool_specs as skills_chat_tool_specs

    for spec in skills_chat_tool_specs():
        name = spec["function"]["name"]
        if name not in seen_names:
            tools.append(spec)
            seen_names.add(name)
    from .cards_mcp import chat_tool_specs as cards_chat_tool_specs

    for spec in cards_chat_tool_specs():
        name = spec["function"]["name"]
        if name not in seen_names:
            tools.append(spec)
            seen_names.add(name)
    return tools


def _tool_dispatch(
    recovery: Recovery,
    doctor_cfg: OpenclawDoctorConfig,
    *,
    hermes_extras: Optional[HermesRecoveryExtras] = None,
    cron_diagnostic_extras: Optional[HermesCronDiagnosticExtras] = None,
) -> dict[str, Callable[[dict], Awaitable[Any]]]:
    restart_name = f"restart_{recovery.cfg.name}"
    dispatch: dict[str, Callable[[dict], Awaitable[Any]]] = {
        "health_check":   lambda _args: recovery.health_check(),
        "process_status": lambda _args: recovery.process_status(),
        "read_log_tail":  lambda args:  recovery.read_log_tail(
            args["path"], int(args.get("lines", 200))
        ),
        restart_name:     lambda _args: recovery.restart(),
        "restart_qclaw":  lambda _args: recovery.restart(),
    }
    if doctor_cfg.enabled:
        dispatch["openclaw_doctor_fix"] = lambda _args: recovery.openclaw_doctor_fix(doctor_cfg)
    if cron_diagnostic_extras is not None and recovery.cfg.name == "hermes":
        dispatch.update(
            _hermes_cron_diagnostic_dispatch(recovery, cron_diagnostic_extras)
        )
    if hermes_extras is not None and recovery.cfg.name == "hermes":
        dispatch.update(_hermes_recovery_dispatch(recovery, hermes_extras))
    from .skills_mcp import _CHAT_TOOL_ACTIONS, dispatch_chat_tool

    for tool_name in _CHAT_TOOL_ACTIONS:
        dispatch[tool_name] = lambda args, n=tool_name: asyncio.to_thread(
            dispatch_chat_tool, n, args
        )
    return dispatch


def _stringify(result: Any) -> str:
    if isinstance(result, (dict, list)):
        return json.dumps(result, default=str)
    return str(result)


async def run_recovery_agent(
    cfg: AgentConfig,
    recovery: Recovery,
    failure_summary: str,
    on_step: Optional[Callable[[Optional[str]], None]] = None,
    doctor_cfg: Optional[OpenclawDoctorConfig] = None,
    *,
    hermes_extras: Optional[HermesRecoveryExtras] = None,
    cron_diagnostic_extras: Optional[HermesCronDiagnosticExtras] = None,
    system_prompt: Optional[str] = None,
    tool_allowlist: Optional[frozenset[str]] = None,
) -> str:
    api_key = os.environ.get(cfg.api_key_env)
    if not api_key:
        raise RuntimeError(
            f"{cfg.api_key_env} not set; cannot call {cfg.base_url} ({cfg.model}). "
            "Put it in ./.env or export it before launching the monitor."
        )

    from .llm_gateway import make_client

    client = make_client(
        api_key=api_key,
        base_url=cfg.base_url,
        timeout=cfg.timeout_seconds,
        is_async=True,
    )
    if doctor_cfg is None:
        doctor_cfg = OpenclawDoctorConfig()
    tools = _tool_catalogue(
        recovery,
        doctor_cfg,
        hermes_extras=hermes_extras,
        cron_diagnostic_extras=cron_diagnostic_extras,
    )
    handlers = _tool_dispatch(
        recovery,
        doctor_cfg,
        hermes_extras=hermes_extras,
        cron_diagnostic_extras=cron_diagnostic_extras,
    )
    if tool_allowlist is not None:
        tools = [t for t in tools if t["function"]["name"] in tool_allowlist]
        handlers = {k: v for k, v in handlers.items() if k in tool_allowlist}

    allowed_logs = ", ".join(recovery.cfg.log_paths) or "(none configured)"
    restart_name = f"restart_{recovery.cfg.name}"
    restart_note = (
        f"{restart_name} is available."
        if recovery.cfg.start_command
        else f"{restart_name} will refuse — no start_command is configured. Diagnose only."
    )
    hermes_note = ""
    if hermes_extras is not None and recovery.cfg.name == "hermes":
        if tool_allowlist is not None:
            hermes_note = (
                "\nCron scheduler tools: "
                + ", ".join(sorted(tool_allowlist))
                + ".\n"
            )
        else:
            hermes_note = (
                "\nHermes tools: hermes_dashboard_status, restart_hermes_dashboard, "
                "hermes_cron_snapshot, hermes_cron_scheduler_status, "
                "ensure_hermes_cron_gateway, hermes_cron_retry.\n"
            )
    user_prompt = (
        f"The {recovery.cfg.name} service is failing health checks.\n\n"
        f"Failure summary:\n{failure_summary}\n\n"
        f"Allowed log files for read_log_tail: {allowed_logs}\n"
        f"{restart_note}{hermes_note}\n"
        "Plan:\n"
        "1. Probe health_check and process_status to confirm the failure mode.\n"
        "2. If a process is running but health fails, inspect log tails for the root cause.\n"
        "3. Restart only when diagnosis indicates it (crashed/missing process, deadlock, OOM, etc).\n"
        "4. After acting, re-run health_check to verify recovery.\n\n"
        "When you are done, reply with a short final report: what you observed, "
        "what you did, and the final health state. Do not call further tools "
        "in that final reply."
    )

    sys_prompt = (system_prompt or cfg.system_prompt or "").strip()
    if hermes_extras is not None and recovery.cfg.name == "hermes" and not system_prompt:
        sys_prompt = default_hermes_recovery_system_prompt()

    messages: list[dict] = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]

    def step(label: Optional[str]) -> None:
        if on_step is not None:
            try:
                on_step(label)
            except Exception:
                pass

    final_text = ""
    for turn in range(cfg.max_tool_iterations):
        step(f"asking {cfg.model} (turn {turn + 1})")
        try:
            resp = await client.chat.completions.create(
                model=cfg.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
        except Exception as e:
            raise RuntimeError(f"{cfg.base_url} call failed: {type(e).__name__}: {e}") from e

        msg = resp.choices[0].message
        # Persist the assistant turn verbatim (including tool_calls if any).
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [tc.model_dump() for tc in (msg.tool_calls or [])],
            }
        )

        if not msg.tool_calls:
            final_text = msg.content or ""
            break

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            step(f"→ {name}")
            handler = handlers.get(name)
            if handler is None:
                tool_result: Any = f"unknown tool: {name}"
            else:
                try:
                    tool_result = await handler(args)
                except Exception as e:
                    tool_result = f"tool {name} raised: {type(e).__name__}: {e}"
            log.info("agent turn %d → tool %s", turn, name)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _stringify(tool_result),
                }
            )
        step(None)
    else:
        final_text = (
            "(agent stopped: hit max_tool_iterations="
            f"{cfg.max_tool_iterations} without a final answer)"
        )

    return final_text.strip() or "(agent produced no text output)"
