"""Minimal MCP stdio client for RuFlo (integration phase 6)."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from typing import Any, Optional

log = logging.getLogger(__name__)


def mcp_available(*, ruflo_bin: str = "npx") -> bool:
    return shutil.which(str(ruflo_bin or "npx")) is not None


class RufloMcpSession:
    """Short-lived JSON-RPC session with ``npx ruflo@latest mcp start``."""

    def __init__(
        self,
        *,
        ruflo_bin: str = "npx",
        ruflo_args: Optional[list[str]] = None,
        project_dir: Optional[str] = None,
        timeout: float = 60.0,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self._timeout = max(5.0, float(timeout or 60.0))
        self._req_id = 0
        self._lock = threading.Lock()
        cmd = [
            str(ruflo_bin or "npx"),
            *list(ruflo_args or ["-y", "ruflo@latest"]),
            "mcp",
            "start",
        ]
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(project_dir or "."),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._initialized = self._handshake()

    def _handshake(self) -> bool:
        resp = self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "gois", "version": "1.0.0"},
            },
        )
        if not resp or resp.get("error"):
            return False
        self._notify("notifications/initialized", {})
        return True

    @property
    def ok(self) -> bool:
        return bool(self._initialized and self._proc.poll() is None)

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        if not self._proc.stdin:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _request(self, method: str, params: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not self._proc.stdin or not self._proc.stdout:
            return None
        req_id = self._next_id()
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        with self._lock:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
            return self._read_response(req_id)

    def _read_response(self, req_id: int) -> Optional[dict[str, Any]]:
        if not self._proc.stdout:
            return None
        deadline = time.time() + self._timeout
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == req_id:
                return msg
        return None

    def call_tool(self, name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if not self.ok:
            return {"ok": False, "error": "mcp session not ready"}
        resp = self._request(
            "tools/call",
            {"name": str(name), "arguments": dict(arguments or {})},
        )
        if not resp:
            return {"ok": False, "error": "mcp timeout"}
        if resp.get("error"):
            err = resp.get("error") or {}
            return {"ok": False, "error": str(err.get("message") or err)}
        result = resp.get("result") or {}
        content = result.get("content") or []
        text = ""
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = str(part.get("text") or "")
                break
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    parsed.setdefault("ok", not bool(result.get("isError")))
                    return parsed
            except json.JSONDecodeError:
                pass
            return {"ok": not bool(result.get("isError")), "output": text}
        return {"ok": True, "result": result}

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        except OSError as exc:
            log.debug("ruflo mcp: close failed: %s", exc)
