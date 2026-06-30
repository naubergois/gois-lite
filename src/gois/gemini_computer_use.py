"""Gemini Computer Use — browser agent loop (Interactions API + Playwright)."""

from __future__ import annotations

import base64
import json
import os
import platform
import time
from typing import Any, Optional

from .roteiro_imagen import resolve_gemini_api_key

DEFAULT_MODEL = "gemini-3.5-flash"
LEGACY_MODEL = "gemini-2.5-computer-use-preview-10-2025"
SUPPORTED_MODELS = frozenset({DEFAULT_MODEL, LEGACY_MODEL, "gemini-3-flash-preview"})
DEFAULT_SCREEN_WIDTH = 1440
DEFAULT_SCREEN_HEIGHT = 900
DEFAULT_MAX_TURNS = 8
DEFAULT_TIMEOUT = 300.0


def denormalize_x(x: int, screen_width: int) -> int:
    return int(x / 1000 * screen_width)


def denormalize_y(y: int, screen_height: int) -> int:
    return int(y / 1000 * screen_height)


def _select_all_key() -> str:
    return "Meta+A" if platform.system() == "Darwin" else "Control+A"


def execute_function_calls(
    interaction: Any,
    page: Any,
    screen_width: int,
    screen_height: int,
) -> list[tuple[str, str, dict[str, Any]]]:
    results: list[tuple[str, str, dict[str, Any]]] = []
    steps = getattr(interaction, "steps", None) or []
    function_calls = [step for step in steps if getattr(step, "type", None) == "function_call"]

    for function_call in function_calls:
        action_result: dict[str, Any] = {}
        fname = str(getattr(function_call, "name", "") or "")
        args = dict(getattr(function_call, "arguments", None) or {})
        call_id = str(getattr(function_call, "id", "") or "")

        try:
            if fname in ("open_web_browser", "open_app"):
                pass
            elif fname in (
                "click",
                "click_at",
                "double_click",
                "triple_click",
                "middle_click",
                "right_click",
                "move",
                "long_press",
            ):
                actual_x = denormalize_x(int(args["x"]), screen_width)
                actual_y = denormalize_y(int(args["y"]), screen_height)
                if fname in ("click", "click_at"):
                    page.mouse.click(actual_x, actual_y)
                elif fname == "double_click":
                    page.mouse.dblclick(actual_x, actual_y)
                elif fname == "right_click":
                    page.mouse.click(actual_x, actual_y, button="right")
                elif fname == "middle_click":
                    page.mouse.click(actual_x, actual_y, button="middle")
                elif fname == "move":
                    page.mouse.move(actual_x, actual_y)
            elif fname in ("type", "type_text_at"):
                actual_x = (
                    denormalize_x(int(args["x"]), screen_width) if "x" in args else None
                )
                actual_y = (
                    denormalize_y(int(args["y"]), screen_height) if "y" in args else None
                )
                text = str(args.get("text") or "")
                press_enter = bool(args.get("press_enter", False))
                if actual_x is not None and actual_y is not None:
                    page.mouse.click(actual_x, actual_y)
                page.keyboard.press(_select_all_key())
                page.keyboard.press("Backspace")
                page.keyboard.type(text)
                if press_enter:
                    page.keyboard.press("Enter")
            elif fname == "navigate":
                page.goto(str(args.get("url") or ""))
            elif fname == "go_back":
                page.go_back()
            elif fname == "go_forward":
                page.go_forward()
            elif fname == "scroll":
                delta_y = int(args.get("delta_y") or args.get("dy") or 0)
                page.mouse.wheel(0, delta_y)
            elif fname == "keypress":
                page.keyboard.press(str(args.get("key") or ""))
            elif fname == "wait":
                time.sleep(float(args.get("seconds") or 1))
            else:
                action_result["warning"] = f"unhandled action: {fname}"

            page.wait_for_load_state(timeout=5000)
            time.sleep(0.5)
        except Exception as exc:
            action_result["error"] = str(exc)[:500]

        results.append((fname, call_id, action_result))

    return results


def get_function_responses(
    page: Any,
    results: list[tuple[str, str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    screenshot_bytes = page.screenshot(type="png")
    current_url = page.url
    function_responses: list[dict[str, Any]] = []
    for name, call_id, result in results:
        function_responses.append(
            {
                "type": "function_result",
                "name": name,
                "call_id": call_id,
                "result": [
                    {
                        "type": "text",
                        "text": json.dumps({"url": current_url, **result}),
                    },
                    {
                        "type": "image",
                        "data": base64.b64encode(screenshot_bytes).decode("utf-8"),
                        "mime_type": "image/png",
                    },
                ],
            }
        )
    return function_responses


def _extract_final_text(interaction: Any) -> str:
    parts: list[str] = []
    for step in getattr(interaction, "steps", None) or []:
        if getattr(step, "type", None) != "model_output":
            continue
        for block in getattr(step, "content", None) or []:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", None)
                if text:
                    parts.append(str(text))
    return " ".join(parts).strip()


def _computer_use_tool_config(*, enable_injection_detection: bool) -> dict[str, Any]:
    return {
        "type": "computer_use",
        "environment": "browser",
        "enable_prompt_injection_detection": enable_injection_detection,
    }


def run_computer_use_agent(
    *,
    task: str,
    start_url: str = "https://www.google.com",
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
    screen_width: int = DEFAULT_SCREEN_WIDTH,
    screen_height: int = DEFAULT_SCREEN_HEIGHT,
    headless: bool = True,
    enable_prompt_injection_detection: bool = True,
    api_key: Optional[str] = None,
) -> dict[str, Any]:
    """Run a Gemini Computer Use loop in Playwright until done or max_turns."""
    prompt = (task or "").strip()
    if not prompt:
        raise ValueError("task is required")

    key = resolve_gemini_api_key(api_key)
    if not key:
        raise ValueError(
            "GEMINI_API_KEY / GOOGLE_API_KEY necessária (Chaves & Secrets ou .env)"
        )

    model_name = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if model_name not in SUPPORTED_MODELS:
        raise ValueError(
            f"model must be one of: {', '.join(sorted(SUPPORTED_MODELS))}"
        )

    try:
        from google import genai
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Dependências em falta. Instale: uv pip install google-genai playwright "
            "&& playwright install chromium"
        ) from exc

    env_backup = {
        "GOOGLE_API_KEY": os.environ.get("GOOGLE_API_KEY"),
        "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY"),
    }
    turns: list[dict[str, Any]] = []
    final_url = start_url
    final_text = ""

    try:
        os.environ.pop("GOOGLE_API_KEY", None)
        os.environ["GEMINI_API_KEY"] = key
        client = genai.Client(api_key=key)
        tool_cfg = _computer_use_tool_config(
            enable_injection_detection=enable_prompt_injection_detection
        )

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context(
                viewport={"width": screen_width, "height": screen_height}
            )
            page = context.new_page()
            page.goto(start_url)
            initial_screenshot = page.screenshot(type="png")

            interaction = client.interactions.create(
                model=model_name,
                input=[
                    {"type": "text", "text": prompt},
                    {
                        "type": "image",
                        "data": base64.b64encode(initial_screenshot).decode("utf-8"),
                        "mime_type": "image/png",
                    },
                ],
                tools=[tool_cfg],
            )

            for turn_idx in range(max(1, int(max_turns))):
                steps = getattr(interaction, "steps", None) or []
                has_calls = any(
                    getattr(step, "type", None) == "function_call" for step in steps
                )
                if not has_calls:
                    final_text = _extract_final_text(interaction)
                    final_url = page.url
                    turns.append(
                        {
                            "turn": turn_idx + 1,
                            "status": "completed",
                            "message": final_text,
                        }
                    )
                    break

                results = execute_function_calls(
                    interaction, page, screen_width, screen_height
                )
                call_intents: dict[str, Any] = {}
                for fc in steps:
                    if getattr(fc, "type", None) != "function_call":
                        continue
                    fc_args = dict(getattr(fc, "arguments", None) or {})
                    call_intents[str(getattr(fc, "id", "") or "")] = fc_args.get(
                        "intent"
                    )
                turns.append(
                    {
                        "turn": turn_idx + 1,
                        "status": "actions_executed",
                        "actions": [
                            {
                                "name": name,
                                "call_id": call_id,
                                "intent": call_intents.get(call_id),
                                "result": result,
                            }
                            for name, call_id, result in results
                        ],
                        "url": page.url,
                    }
                )

                function_responses = get_function_responses(page, results)
                interaction = client.interactions.create(
                    model=model_name,
                    previous_interaction_id=interaction.id,
                    input=function_responses,
                    tools=[tool_cfg],
                )
                final_url = page.url
            else:
                final_text = _extract_final_text(interaction)
                turns.append({"turn": len(turns) + 1, "status": "max_turns_reached"})

            browser.close()
    finally:
        if env_backup["GOOGLE_API_KEY"] is None:
            os.environ.pop("GOOGLE_API_KEY", None)
        else:
            os.environ["GOOGLE_API_KEY"] = env_backup["GOOGLE_API_KEY"]
        if env_backup["GEMINI_API_KEY"] is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = env_backup["GEMINI_API_KEY"]

    return {
        "ok": True,
        "model_used": model_name,
        "task": prompt,
        "start_url": start_url,
        "final_url": final_url,
        "final_message": final_text,
        "turns": turns,
        "turn_count": len(turns),
    }


def dispatch_gemini_computer_use(args: dict[str, Any]) -> dict[str, Any]:
    task = str(args.get("task") or args.get("prompt") or args.get("input") or "").strip()
    if not task:
        return {"ok": False, "error": "task is required"}

    try:
        max_turns = int(args.get("max_turns") or DEFAULT_MAX_TURNS)
    except (TypeError, ValueError):
        max_turns = DEFAULT_MAX_TURNS

    try:
        screen_width = int(args.get("screen_width") or DEFAULT_SCREEN_WIDTH)
    except (TypeError, ValueError):
        screen_width = DEFAULT_SCREEN_WIDTH

    try:
        screen_height = int(args.get("screen_height") or DEFAULT_SCREEN_HEIGHT)
    except (TypeError, ValueError):
        screen_height = DEFAULT_SCREEN_HEIGHT

    headless_raw = args.get("headless")
    headless = True if headless_raw is None else bool(headless_raw)

    try:
        result = run_computer_use_agent(
            task=task,
            start_url=str(args.get("start_url") or "https://www.google.com").strip(),
            model=str(args.get("model") or DEFAULT_MODEL).strip(),
            max_turns=max(1, min(max_turns, 25)),
            screen_width=max(800, min(screen_width, 2560)),
            screen_height=max(600, min(screen_height, 1600)),
            headless=headless,
            enable_prompt_injection_detection=bool(
                args.get("enable_prompt_injection_detection", True)
            ),
            api_key=str(args.get("api_key") or "").strip() or None,
        )
        return result
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:800]}


def mcp_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "gemini_computer_use",
            "description": (
                "Agente visual Gemini Computer Use + Playwright: screenshot → ação "
                "(click, type, scroll) → loop até concluir. Requer GEMINI_API_KEY. "
                "Skill: qclaw-gemini-computer-use. Equivalente a "
                "qclaw_gemini_computer_use no chat."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Tarefa em linguagem natural para o agente",
                    },
                    "start_url": {
                        "type": "string",
                        "description": "URL inicial do browser (default google.com)",
                    },
                    "model": {
                        "type": "string",
                        "enum": sorted(SUPPORTED_MODELS),
                        "description": "gemini-3.5-flash recomendado",
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": "Máximo de passos do loop (default 8, max 25)",
                    },
                    "headless": {
                        "type": "boolean",
                        "description": "Browser sem UI (default true)",
                    },
                    "screen_width": {"type": "integer"},
                    "screen_height": {"type": "integer"},
                    "enable_prompt_injection_detection": {
                        "type": "boolean",
                        "description": "Detecção de prompt injection em screenshots",
                    },
                },
                "required": ["task"],
            },
        }
    ]
