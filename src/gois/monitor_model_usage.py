"""Per-model daily token usage parsing from Hermes agent logs."""

from __future__ import annotations

import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .hermes_cron import resolve_agent_log_path

MODEL_DAILY_USAGE_CACHE_SECONDS = 60.0
MODEL_DAILY_USAGE_MAX_LOG_LINES = 200000

CRON_SESSION_DAY_RE = re.compile(
    r"\[(cron_[a-f0-9\-]+_(?P<day>\d{8})_\d{6})\]",
    re.IGNORECASE,
)
CRON_TOKEN_USAGE_RE = re.compile(
    r"model=(?P<model>\S+)\s+context=~(?P<tok>[\d,]+)\s+tokens",
    re.IGNORECASE,
)
CRON_API_CALL_TOKEN_RE = re.compile(
    r"API call #\d+:\s+model=(?P<model>\S+)\s+.*?\btotal=(?P<tok>[\d,]+)",
    re.IGNORECASE,
)

class MonitorModelUsageMixin:
    @staticmethod
    def _today_key() -> str:
        return datetime.now().astimezone().date().isoformat()

    @staticmethod
    def _parse_yyyymmdd(day_raw: str) -> Optional[str]:
        try:
            dt = datetime.strptime(day_raw, "%Y%m%d")
            return dt.date().isoformat()
        except Exception:
            return None

    @staticmethod
    def _read_log_tail_lines(path: Optional[Path], max_lines: int) -> list[str]:
        if path is None:
            return []
        try:
            tail = deque(maxlen=max(1, max_lines))
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    tail.append(line)
            return list(tail)
        except OSError:
            return []

    def _coerce_model_daily_quotas(self) -> dict[str, int]:
        raw = self.state.model_daily_token_quotas
        if not isinstance(raw, dict):
            return {}
        out: dict[str, int] = {}
        for key, value in raw.items():
            model = str(key or "").strip()
            if not model:
                continue
            try:
                limit = int(value)
            except Exception:
                continue
            if limit > 0:
                out[model] = limit
        return out

    def _coerce_model_daily_usd_quotas(self) -> dict[str, float]:
        raw = self.state.model_daily_usd_quotas
        if not isinstance(raw, dict):
            return {}
        out: dict[str, float] = {}
        for key, value in raw.items():
            model = str(key or "").strip()
            if not model:
                continue
            try:
                limit = float(value)
            except Exception:
                continue
            if limit > 0:
                out[model] = round(limit, 6)
        return out

    def _coerce_model_usd_per_1k_prices(self) -> dict[str, float]:
        raw = self.state.model_usd_per_1k_prices
        if not isinstance(raw, dict):
            return {}
        out: dict[str, float] = {}
        for key, value in raw.items():
            model = str(key or "").strip()
            if not model:
                continue
            try:
                price = float(value)
            except Exception:
                continue
            if price > 0:
                out[model] = round(price, 6)
        return out

    def _compute_model_daily_usage(
        self,
        *,
        day_iso: str,
        agent_log_path: Optional[Path],
        max_log_lines: int = MODEL_DAILY_USAGE_MAX_LOG_LINES,
    ) -> dict[str, Any]:
        lines = self._read_log_tail_lines(agent_log_path, max_log_lines)
        if not lines:
            return {
                "day": day_iso,
                "models": {},
                "total_tokens": 0,
                "total_runs": 0,
                "total_sessions": 0,
            }

        # session_id -> (model, max_tokens)
        per_session: dict[str, tuple[str, int]] = {}
        for line in lines:
            session_match = CRON_SESSION_DAY_RE.search(line)
            if not session_match:
                continue
            run_day = self._parse_yyyymmdd(session_match.group("day"))
            if run_day != day_iso:
                continue
            token_match = CRON_API_CALL_TOKEN_RE.search(line) or CRON_TOKEN_USAGE_RE.search(line)
            if not token_match:
                continue
            try:
                tokens = int(token_match.group("tok").replace(",", ""))
            except Exception:
                continue
            if tokens <= 0:
                continue
            session_id = str(session_match.group(1) or "")
            model = str(token_match.group("model") or "desconhecido").strip() or "desconhecido"
            prev = per_session.get(session_id)
            if prev is None or tokens > prev[1]:
                per_session[session_id] = (model, tokens)

        models: dict[str, dict[str, Any]] = {}
        total_tokens = 0
        total_runs = 0
        for model, tokens in per_session.values():
            row = models.setdefault(
                model,
                {"model": model, "total_tokens": 0, "runs": 0, "max_tokens": 0},
            )
            row["total_tokens"] += tokens
            row["runs"] += 1
            if tokens > row["max_tokens"]:
                row["max_tokens"] = tokens
            total_tokens += tokens
            total_runs += 1

        for row in models.values():
            runs = int(row.get("runs") or 0)
            total = int(row.get("total_tokens") or 0)
            row["avg_tokens"] = int(round(total / runs)) if runs > 0 else 0

        return {
            "day": day_iso,
            "models": models,
            "total_tokens": total_tokens,
            "total_runs": total_runs,
            "total_sessions": len(per_session),
        }

    def _cached_model_daily_usage(self, *, day_iso: Optional[str] = None) -> dict[str, Any]:
        target_day = day_iso or self._today_key()
        now = time.time()
        with self._model_daily_usage_cache_lock:
            if (
                self._model_daily_usage_cache is not None
                and self._model_daily_usage_cache_day == target_day
                and now < self._model_daily_usage_cache_expires_at
            ):
                return dict(self._model_daily_usage_cache)

        agent_log_path = resolve_agent_log_path(
            self.cfg.hermes.log_paths if self.cfg.hermes else None
        )
        snapshot = self._compute_model_daily_usage(
            day_iso=target_day,
            agent_log_path=agent_log_path,
        )
        with self._model_daily_usage_cache_lock:
            self._model_daily_usage_cache_day = target_day
            self._model_daily_usage_cache = dict(snapshot)
            self._model_daily_usage_cache_expires_at = now + MODEL_DAILY_USAGE_CACHE_SECONDS
        return dict(snapshot)
