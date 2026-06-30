#!/usr/bin/env python3
"""Write minimal MCP config for gois-lite (gois-cards only)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def build_lite_mcp_json(*, target: Path, python_bin: str) -> dict:
    src = target / "src"
    return {
        "mcpServers": {
            "gois-cards": {
                "command": python_bin,
                "args": ["-m", "gois.mcp_cards_server"],
                "env": {
                    "PYTHONPATH": str(src),
                    "GOIS_LITE": "1",
                    "GOIS_LITE_DB_BACKEND": "sqlite",
                    "GOIS_STACK_ROOT": str(target / ".stack"),
                    "GOIS_KANBAN_WORKDIRS": str(target),
                },
                "autoApprove": [
                    "list_kanban_boards",
                    "list_teams",
                    "get_cards",
                    "get_card_detail",
                    "get_my_cards",
                    "get_cards_todo",
                    "create_card",
                    "move_card",
                    "update_card",
                    "kanban_ide_handoff",
                ],
            },
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--python-bin", type=str, required=True)
    args = parser.parse_args()

    payload = build_lite_mcp_json(
        target=args.target.resolve(),
        python_bin=args.python_bin,
    )
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    for rel in (".cursor/mcp.json", ".mcp.json", ".kiro/settings/mcp.json"):
        path = args.target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
