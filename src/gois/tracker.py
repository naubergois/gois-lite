"""Lightweight in-process tracking of named async "agents" (health-loop,
reaper-loop, recovery-agent, ...). Exposed via /status so the dashboard can
show which ones are running right now.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import AsyncIterator, Iterable, Optional


@dataclass
class AgentRun:
    name: str
    description: str = ""              # one-line "what this agent does"
    enabled: bool = True                # operators can pause an agent at runtime
    state: str = "idle"                # idle | running | error | paused
    started_at: Optional[float] = None # set while state == running
    last_started_at: Optional[float] = None
    last_finished_at: Optional[float] = None
    last_duration_seconds: Optional[float] = None
    runs_total: int = 0
    last_result: Optional[str] = None  # short human-readable summary
    last_error: Optional[str] = None
    current_step: Optional[str] = None # live sub-step while running (eg. "→ read_log_tail")
    last_step: Optional[str] = None    # last step from the previous run


class AgentTracker:
    def __init__(self, names: Iterable[str] = ()):
        self._agents: dict[str, AgentRun] = {n: AgentRun(name=n) for n in names}

    def register(self, name: str, description: str) -> AgentRun:
        a = self.get(name)
        a.description = description
        return a

    def get(self, name: str) -> AgentRun:
        a = self._agents.get(name)
        if a is None:
            a = AgentRun(name=name)
            self._agents[name] = a
        return a

    def set_step(self, name: str, step: Optional[str]) -> None:
        a = self.get(name)
        a.current_step = step
        if step is not None:
            a.last_step = step

    @asynccontextmanager
    async def track(self, name: str) -> AsyncIterator[AgentRun]:
        a = self.get(name)
        start = time.time()
        a.state = "running"
        a.started_at = start
        a.last_started_at = start
        a.last_error = None
        try:
            yield a
        except BaseException as e:
            a.last_error = f"{type(e).__name__}: {e}"[:240]
            a.state = "error"
            raise
        else:
            a.state = "idle"
        finally:
            end = time.time()
            a.last_finished_at = end
            a.last_duration_seconds = end - start
            a.started_at = None
            a.current_step = None
            a.runs_total += 1

    def snapshot(self) -> list[dict]:
        # Stable order = insertion order = registration order.
        return [asdict(a) for a in self._agents.values()]
