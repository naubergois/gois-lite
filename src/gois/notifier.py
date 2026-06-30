"""Structured logging + optional webhook notifications."""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

from .config import NotifierConfig

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: NotifierConfig):
        self.cfg = cfg
        if cfg.log_file:
            handler = logging.FileHandler(cfg.log_file)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
            )
            logging.getLogger().addHandler(handler)

    async def notify(self, level: str, title: str, body: Optional[str] = None) -> None:
        log_fn = getattr(log, level.lower(), log.info)
        log_fn("%s | %s", title, (body or "").replace("\n", " ⏎ ")[:1000])
        if not self.cfg.webhook_url:
            return
        payload = {"text": f"*{title}*\n{body or ''}"[:3500]}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(self.cfg.webhook_url, json=payload)
        except Exception as e:
            log.warning("webhook delivery failed: %s", e)

    def dump(self, obj: object) -> str:
        try:
            return json.dumps(obj, default=str, ensure_ascii=False)
        except Exception:
            return str(obj)
