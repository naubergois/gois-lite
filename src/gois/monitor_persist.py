"""Persist monitor state and sync metrics."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)



class MonitorPersistMixin:

    def _persist(self) -> None:
        self.state.save(self._state_path)

    def _sync_metrics_from_state(self) -> None:
        if not self.metrics:
            return
        self.metrics.consecutive_failures.set(self.state.consecutive_failures)
        if self.state.last_recovery_ts:
            self.metrics.last_recovery_ts.set(self.state.last_recovery_ts)
        if self.state.last_health_ok_ts:
            self.metrics.last_health_ok_ts.set(self.state.last_health_ok_ts)
        if self.cfg.hermes:
            self.metrics.hermes_consecutive_failures.set(
                self.state.hermes_consecutive_failures or 0
            )
            if self.state.hermes_last_health_ok_ts:
                up = (
                    self.state.hermes_consecutive_failures == 0
                )
