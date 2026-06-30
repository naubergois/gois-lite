"""Priority queue engine for kanban cards with skills.

Cards are enqueued with a priority (lower number = higher priority).
The queue processor picks the highest-priority card, runs it via
the kanban schedule mechanism, and advances to the next card when
the current one finishes (done) or quota is exhausted.

Management is exposed both through the dashboard UI and chat commands.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from .runtime_state import load_json, save_json, runtime_redis_enabled

log = logging.getLogger(__name__)

_PRIORITY_QUEUE_KEY = "priority_queue:state"

_MAX_QUEUE_SIZE = 200
_HISTORY_TTL_SECONDS = 86400.0  # 24h
_RECENT_DONE_SUPPRESS_SECONDS = 900.0  # 15 min — evita reenfileirar o mesmo card
_STALE_RUNNING_SECONDS = 600.0  # 10 min — recupera cards presos em running
_EXECUTION_TIMEOUT_SECONDS = 600.0  # 10 min — watchdog: abandona execução pendurada
_MAX_FINISHED_HISTORY = 80  # limita cards concluídos/erro retidos (evita state.json gigante)


class CardStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    PAUSED = "paused"
    QUOTA_EXCEEDED = "quota_exceeded"


@dataclass
class PriorityCard:
    """A card in the priority queue."""
    id: str
    task_id: str
    title: str
    priority: int  # lower = runs first
    skills: list[str] = field(default_factory=list)
    assignee: str = ""
    workdir: str = ""
    kanban_file: Optional[str] = None
    team_id: str = ""
    status: CardStatus = CardStatus.QUEUED
    progress: list[str] = field(default_factory=list)
    last_progress: str = ""
    error: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    schedule_job_id: Optional[str] = None
    enqueued_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    # LLM model override (None = use global default)
    model_id: Optional[str] = None
    # Retry / quota tracking
    retry_count: int = 0
    max_retries: int = 3


@dataclass
class QueueState:
    """Persistent queue state."""
    cards: list[PriorityCard] = field(default_factory=list)
    running_card_id: Optional[str] = None
    paused: bool = False
    quota_exceeded: bool = False
    last_quota_check: float = 0.0
    total_completed: int = 0
    total_errors: int = 0


class PriorityQueueEngine:
    """Thread-safe priority queue for kanban cards."""

    def __init__(
        self,
        state_path: Optional[Path] = None,
        quota_checker: Optional[Callable[[], bool]] = None,
        schedule_runner: Optional[Callable[[dict], dict]] = None,
        on_terminal_failure: Optional[Callable[[PriorityCard], None]] = None,
        on_card_done: Optional[Callable[[PriorityCard], None]] = None,
        on_card_start: Optional[Callable[[PriorityCard], None]] = None,
        execution_timeout_seconds: float = _EXECUTION_TIMEOUT_SECONDS,
    ):
        self._lock = threading.Lock()
        self._state = QueueState()
        self._state_path = state_path
        self._quota_checker = quota_checker  # returns True if quota exceeded
        self._schedule_runner = schedule_runner
        self._on_terminal_failure = on_terminal_failure
        self._on_card_done = on_card_done
        self._on_card_start = on_card_start
        self._execution_timeout = max(1.0, float(execution_timeout_seconds))
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._load_state()

    # ── Public API ──────────────────────────────────────────────────────

    def enqueue(
        self,
        task_id: str,
        title: str,
        priority: int,
        *,
        skills: Optional[list[str]] = None,
        assignee: str = "",
        workdir: str = "",
        kanban_file: Optional[str] = None,
        team_id: str = "",
        model_id: Optional[str] = None,
        max_retries: int = 3,
    ) -> PriorityCard:
        """Add a card to the priority queue. Wakes the worker if idle."""
        card = PriorityCard(
            id=uuid.uuid4().hex[:12],
            task_id=task_id.strip(),
            title=title.strip(),
            priority=max(1, min(priority, 10)),
            skills=list(skills or []),
            assignee=assignee.strip(),
            workdir=workdir.strip(),
            kanban_file=kanban_file,
            team_id=team_id.strip(),
            model_id=model_id.strip() if model_id else None,
            max_retries=max_retries,
        )
        with self._lock:
            # Check duplicates (both queued and running)
            if any(
                c.task_id == card.task_id
                and c.status in (CardStatus.QUEUED, CardStatus.RUNNING)
                for c in self._state.cards
            ):
                raise ValueError(f"tarefa {card.task_id} já está na fila")
            if self._has_recent_done(card.task_id):
                raise ValueError(
                    f"tarefa {card.task_id} foi agendada recentemente — "
                    "aguardando execução Hermes"
                )
            if len(self._state.cards) >= _MAX_QUEUE_SIZE:
                self._prune_finished()
            self._state.cards.append(card)
            self._sort_queue()
            self._persist()
        self._wake_event.set()
        return card

    def update_priority(self, card_id: str, new_priority: int) -> PriorityCard:
        """Change priority of a queued card."""
        with self._lock:
            card = self._find_card(card_id)
            if card is None:
                raise ValueError(f"card {card_id} não encontrado")
            if card.status != CardStatus.QUEUED:
                raise ValueError(f"card {card_id} não está na fila (status={card.status})")
            card.priority = max(1, min(new_priority, 10))
            self._sort_queue()
            self._persist()
            return card

    def update_model(self, card_id: str, model_id: Optional[str]) -> PriorityCard:
        """Change the LLM model of a queued card."""
        with self._lock:
            card = self._find_card(card_id)
            if card is None:
                raise ValueError(f"card {card_id} não encontrado")
            if card.status != CardStatus.QUEUED:
                raise ValueError(f"card {card_id} não está na fila (status={card.status})")
            card.model_id = model_id.strip() if model_id else None
            self._persist()
            return card

    def remove(self, card_id: str) -> PriorityCard:
        """Remove a card from the queue."""
        with self._lock:
            card = self._find_card(card_id)
            if card is None:
                raise ValueError(f"card {card_id} não encontrado")
            if card.status == CardStatus.RUNNING:
                raise ValueError(f"card {card_id} está em execução — pause primeiro")
            self._state.cards = [c for c in self._state.cards if c.id != card_id]
            self._persist()
            return card

    def cancel_running(self, card_id: str) -> PriorityCard:
        """Stop a running priority-queue card and its underlying schedule job."""
        schedule_job_id: Optional[str] = None
        with self._lock:
            card = self._find_card(card_id)
            if card is None:
                raise ValueError(f"card {card_id} não encontrado")
            if card.status != CardStatus.RUNNING:
                raise ValueError(f"card {card_id} não está em execução")
            schedule_job_id = card.schedule_job_id
        if schedule_job_id:
            try:
                from .kanban_schedule_jobs import cancel_job as ks_cancel

                ks_cancel(schedule_job_id)
            except Exception as exc:
                log.debug("priority queue: schedule cancel %s failed: %s", schedule_job_id, exc)
        with self._lock:
            card = self._find_card(card_id)
            if card is None or card.status != CardStatus.RUNNING:
                raise ValueError(f"card {card_id} não está em execução")
            card.status = CardStatus.ERROR
            card.error = "Execução cancelada pelo usuário"
            card.finished_at = time.time()
            card.last_progress = "Execução cancelada pelo usuário"
            card.progress.append(card.last_progress)
            self._state.running_card_id = None
            self._state.total_errors += 1
            self._persist()
            return card

    def pause_queue(self) -> None:
        """Pause the queue processing (current card finishes, no new starts)."""
        with self._lock:
            self._state.paused = True
            self._persist()

    def resume_queue(self) -> None:
        """Resume queue processing."""
        with self._lock:
            self._state.paused = False
            self._state.quota_exceeded = False
            self._persist()
        self._wake_event.set()

    def reorder(self, card_ids: list[str]) -> None:
        """Set explicit priority order based on list position."""
        with self._lock:
            queued = [c for c in self._state.cards if c.status == CardStatus.QUEUED]
            id_set = {c.id for c in queued}
            for i, cid in enumerate(card_ids):
                if cid not in id_set:
                    continue
                card = next(c for c in queued if c.id == cid)
                card.priority = i + 1
            self._sort_queue()
            self._persist()

    def get_queue(self) -> dict[str, Any]:
        """Return current queue state as JSON-serializable dict."""
        with self._lock:
            by_status = {}
            for c in self._state.cards:
                by_status.setdefault(c.status, []).append(c)
            queued = by_status.get(CardStatus.QUEUED, [])
            running = by_status.get(CardStatus.RUNNING, [])
            done = by_status.get(CardStatus.DONE, [])
            errors = by_status.get(CardStatus.ERROR, []) + by_status.get(CardStatus.QUOTA_EXCEEDED, [])
            return {
                "ok": True,
                "paused": self._state.paused,
                "quota_exceeded": self._state.quota_exceeded,
                "total_completed": self._state.total_completed,
                "total_errors": self._state.total_errors,
                "queued": [self._card_to_dict(c) for c in queued],
                "running": [self._card_to_dict(c) for c in running],
                "done": [self._card_to_dict(c) for c in done[-20:]],  # last 20
                "errors": [self._card_to_dict(c) for c in errors[-10:]],
            }

    def get_card(self, card_id: str) -> Optional[dict[str, Any]]:
        """Get a single card by queue ID."""
        with self._lock:
            card = self._find_card(card_id)
            if card is None:
                return None
            return self._card_to_dict(card)

    def recent_error_for_task(
        self, task_id: str, within_seconds: float
    ) -> Optional[str]:
        """Return the most recent terminal error for a task within a window.

        Used as a circuit breaker so the auto-scheduler stops re-enqueueing a
        card that keeps failing for the same reason (e.g. missing Hermes
        profile). Returns ``None`` when there is no fresh terminal failure.
        """
        needle = str(task_id or "").strip()
        if not needle or within_seconds <= 0:
            return None
        now = time.time()
        latest_ts = 0.0
        latest_err: Optional[str] = None
        with self._lock:
            for card in self._state.cards:
                if card.task_id != needle or card.status != CardStatus.ERROR:
                    continue
                finished = float(card.finished_at or 0.0)
                if not finished or (now - finished) > within_seconds:
                    continue
                if finished >= latest_ts:
                    latest_ts = finished
                    latest_err = (card.error or "execução falhou").strip()
        return latest_err

    def last_error_by_task(self) -> dict[str, str]:
        """Map task_id -> most recent terminal error message (any age)."""
        out: dict[str, tuple[float, str]] = {}
        with self._lock:
            for card in self._state.cards:
                if card.status != CardStatus.ERROR:
                    continue
                ts = float(card.finished_at or card.enqueued_at or 0.0)
                err = (card.error or "execução falhou").strip()
                prev = out.get(card.task_id)
                if prev is None or ts >= prev[0]:
                    out[card.task_id] = (ts, err)
        return {task_id: err for task_id, (_, err) in out.items()}

    def last_executor_by_task(self) -> dict[str, tuple[float, str]]:
        """Map task_id -> (finished_at, assignee slug) for completed runs."""
        out: dict[str, tuple[float, str]] = {}
        with self._lock:
            for card in self._state.cards:
                if card.status not in (CardStatus.DONE, CardStatus.ERROR):
                    continue
                assignee = str(card.assignee or "").strip()
                if not assignee:
                    continue
                ts = float(card.finished_at or card.started_at or card.enqueued_at or 0.0)
                prev = out.get(card.task_id)
                if prev is None or ts >= prev[0]:
                    out[card.task_id] = (ts, assignee)
        return out

    # ── Worker loop ─────────────────────────────────────────────────────

    def start_worker(self) -> None:
        """Start background worker that processes the queue."""
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="priority-queue-worker",
            daemon=True,
        )
        self._worker_thread.start()
        log.info("priority queue worker started")

    def stop_worker(self) -> None:
        """Stop the worker gracefully."""
        self._stop_event.set()
        self._wake_event.set()
        if self._worker_thread:
            self._worker_thread.join(timeout=10.0)
        log.info("priority queue worker stopped")

    def _worker_loop(self) -> None:
        """Main worker loop: pick highest-priority card, run it, repeat."""
        while not self._stop_event.is_set():
            self._wake_event.clear()

            # Check if paused
            with self._lock:
                if self._state.paused:
                    log.debug("queue paused, waiting for resume")
                    self._wake_event.wait(timeout=30.0)
                    continue

            # Check quota
            if self._quota_checker and self._quota_checker():
                with self._lock:
                    self._state.quota_exceeded = True
                    self._persist()
                log.info("quota exceeded, pausing queue processing")
                self._wake_event.wait(timeout=60.0)
                continue
            else:
                with self._lock:
                    self._state.quota_exceeded = False

            self._recover_stale_running_cards()

            # Pick next card
            card = self._pick_next_card()
            if card is None:
                # No work to do, wait for wake
                self._wake_event.wait(timeout=30.0)
                continue

            # Execute the card
            self._execute_card(card)

    def _pick_next_card(self) -> Optional[PriorityCard]:
        """Select the highest-priority queued card."""
        notify: Optional[Callable[[PriorityCard], None]] = None
        picked: Optional[PriorityCard] = None
        with self._lock:
            for card in self._state.cards:
                if card.status == CardStatus.QUEUED:
                    card.status = CardStatus.RUNNING
                    card.started_at = time.time()
                    self._state.running_card_id = card.id
                    self._persist()
                    picked = card
                    notify = self._on_card_start
                    break
        if picked is None:
            return None
        if notify is not None:
            try:
                notify(picked)
            except Exception as exc:  # noqa: BLE001 — callback must not break the worker
                log.warning("priority queue on_card_start failed: %s", exc)
        return picked

    def _execute_card(self, card: PriorityCard) -> None:
        """Run a card via the schedule mechanism."""
        import os

        log.info(
            "executing priority card %s (task=%s, priority=%d, skills=%s)",
            card.id, card.task_id, card.priority, card.skills,
        )
        self._append_progress(card, f"Iniciando execução (prioridade {card.priority})…")

        if not self._schedule_runner:
            self._mark_error(card, "schedule_runner não configurado")
            return

        payload = {
            "task_id": card.task_id,
            "assignee": card.assignee,
            "workdir": card.workdir,
            "skills": card.skills,
            "once": True,
            "schedule": "1m",
            "async": False,
        }
        if card.kanban_file:
            payload["kanban_file"] = card.kanban_file
        if card.team_id:
            payload["team_id"] = card.team_id
        if card.model_id:
            payload["model_id"] = card.model_id
        if card.model_id:
            payload["model_id"] = card.model_id

        # Watchdog: executa o schedule numa thread própria para que uma
        # execução pendurada nunca trave a fila inteira (tasks que nunca
        # começam porque o worker ficou bloqueado num card anterior).
        outcome: dict[str, Any] = {}

        def _run() -> None:
            prev = os.environ.get("QCLAW_SCHEDULED_JOB")
            os.environ["QCLAW_SCHEDULED_JOB"] = "1"
            try:
                outcome["result"] = self._schedule_runner(payload)
            except Exception as e:  # noqa: BLE001 — repassado ao chamador
                outcome["exception"] = e
            finally:
                if prev is None:
                    os.environ.pop("QCLAW_SCHEDULED_JOB", None)
                else:
                    os.environ["QCLAW_SCHEDULED_JOB"] = prev

        runner = threading.Thread(
            target=_run,
            name=f"priority-card-{card.id}",
            daemon=True,
        )
        runner.start()
        runner.join(timeout=self._execution_timeout)

        if runner.is_alive():
            log.warning(
                "priority card %s (task=%s) exceeded %.0fs — abandoning execution",
                card.id, card.task_id, self._execution_timeout,
            )
            self._mark_error(
                card,
                f"execução excedeu {int(self._execution_timeout)}s — "
                "abandonada pelo watchdog",
            )
            return

        if "exception" in outcome:
            e = outcome["exception"]
            log.error("priority card %s failed: %s", card.id, e)
            self._mark_error(card, f"{type(e).__name__}: {e}")
            return

        result = outcome.get("result")
        if not isinstance(result, dict):
            result = {"ok": False, "error": "schedule_runner não retornou resultado"}
        if result.get("ok"):
            self._mark_done(card, result)
        else:
            error_msg = str(result.get("error") or "execução falhou")
            if "cota" in error_msg.lower() or "quota" in error_msg.lower():
                self._mark_quota_exceeded(card)
            else:
                self._mark_error(card, error_msg, result=result)

    def _mark_done(self, card: PriorityCard, result: dict) -> None:
        notify: Optional[Callable[[PriorityCard], None]] = None
        with self._lock:
            card.status = CardStatus.DONE
            card.finished_at = time.time()
            card.result = result
            card.last_progress = "Agendamento Hermes concluído ✓"
            card.progress.append(card.last_progress)
            self._state.running_card_id = None
            self._state.total_completed += 1
            self._persist()
            notify = self._on_card_done
        log.info("card %s (task=%s) completed", card.id, card.task_id)
        if notify is not None:
            try:
                notify(card)
            except Exception as exc:  # noqa: BLE001 — callback must not break the worker
                log.warning("priority queue on_card_done failed: %s", exc)

    def _mark_error(self, card: PriorityCard, error: str, *, result: Optional[dict] = None) -> None:
        notify: Optional[Callable[[PriorityCard], None]] = None
        with self._lock:
            if card.retry_count < card.max_retries:
                card.retry_count += 1
                card.status = CardStatus.QUEUED
                card.started_at = None
                card.last_progress = f"Erro ({card.retry_count}/{card.max_retries}): {error}. Reenfileirando…"
                card.progress.append(card.last_progress)
                log.warning("card %s retrying (%d/%d): %s", card.id, card.retry_count, card.max_retries, error)
            else:
                card.status = CardStatus.ERROR
                card.finished_at = time.time()
                card.error = error
                card.result = result
                card.last_progress = f"Falhou após {card.max_retries} tentativas: {error}"
                card.progress.append(card.last_progress)
                self._state.total_errors += 1
                log.error("card %s (task=%s) permanently failed: %s", card.id, card.task_id, error)
                notify = self._on_terminal_failure
            self._state.running_card_id = None
            self._persist()
        if notify is not None:
            try:
                notify(card)
            except Exception as exc:  # noqa: BLE001 — callback must not break the worker
                log.warning("priority queue on_terminal_failure failed: %s", exc)

    def _mark_quota_exceeded(self, card: PriorityCard) -> None:
        with self._lock:
            card.status = CardStatus.QUEUED  # re-enqueue, don't count as error
            card.started_at = None
            card.last_progress = "Cota excedida — aguardando liberação de cota"
            card.progress.append(card.last_progress)
            self._state.running_card_id = None
            self._state.quota_exceeded = True
            self._persist()
        log.info("card %s paused due to quota — queue paused", card.id)

    def _append_progress(self, card: PriorityCard, msg: str) -> None:
        with self._lock:
            card.progress.append(msg)
            card.last_progress = msg

    # ── Internals ───────────────────────────────────────────────────────

    def _has_recent_done(self, task_id: str) -> bool:
        now = time.time()
        for card in self._state.cards:
            if card.task_id != task_id or card.status != CardStatus.DONE:
                continue
            finished = card.finished_at or card.enqueued_at
            if finished and (now - float(finished)) < _RECENT_DONE_SUPPRESS_SECONDS:
                return True
        return False

    def _recover_stale_running_cards(self) -> None:
        """Re-queue cards stuck in running (worker crash or hung schedule)."""
        now = time.time()
        with self._lock:
            changed = False
            for card in self._state.cards:
                if card.status != CardStatus.RUNNING:
                    continue
                started = float(card.started_at or 0.0)
                if not started or (now - started) <= _STALE_RUNNING_SECONDS:
                    continue
                card.status = CardStatus.QUEUED
                card.started_at = None
                card.last_progress = "Execução expirou — reenfileirando…"
                card.progress.append(card.last_progress)
                changed = True
                log.warning(
                    "priority card %s (%s) stale running — re-queued",
                    card.id,
                    card.task_id,
                )
            if not changed:
                return
            self._state.running_card_id = None
            self._persist()
        self._wake_event.set()

    def _find_card(self, card_id: str) -> Optional[PriorityCard]:
        for card in self._state.cards:
            if card.id == card_id:
                return card
        return None

    def _sort_queue(self) -> None:
        """Sort queued cards by priority (ascending = higher priority first)."""
        queued = [c for c in self._state.cards if c.status == CardStatus.QUEUED]
        non_queued = [c for c in self._state.cards if c.status != CardStatus.QUEUED]
        queued.sort(key=lambda c: (c.priority, c.enqueued_at))
        self._state.cards = queued + non_queued

    def _prune_finished(self) -> None:
        """Remove old finished cards to stay within size limits.

        Keeps active cards, drops finished cards past the TTL, and caps the
        retained finished-card history so a runaway re-enqueue loop can't bloat
        the persisted state to megabytes.
        """
        now = time.time()
        active = [
            c for c in self._state.cards
            if c.status in (CardStatus.QUEUED, CardStatus.RUNNING)
        ]
        finished = [
            c for c in self._state.cards
            if c.status not in (CardStatus.QUEUED, CardStatus.RUNNING)
            and c.finished_at is not None
            and (now - c.finished_at) < _HISTORY_TTL_SECONDS
        ]
        if len(finished) > _MAX_FINISHED_HISTORY:
            finished.sort(key=lambda c: float(c.finished_at or 0.0))
            finished = finished[-_MAX_FINISHED_HISTORY:]
        self._state.cards = active + finished

    def _card_to_dict(self, card: PriorityCard) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": card.id,
            "task_id": card.task_id,
            "title": card.title,
            "priority": card.priority,
            "skills": card.skills,
            "assignee": card.assignee,
            "status": card.status.value,
            "last_progress": card.last_progress,
            "progress": list(card.progress[-10:]),
            "enqueued_at": card.enqueued_at,
            "retry_count": card.retry_count,
            "max_retries": card.max_retries,
        }
        if card.model_id:
            out["model_id"] = card.model_id
        if card.workdir:
            out["workdir"] = card.workdir
        if card.team_id:
            out["team_id"] = card.team_id
        if card.started_at:
            out["started_at"] = card.started_at
        if card.finished_at:
            out["finished_at"] = card.finished_at
        if card.error:
            out["error"] = card.error
        if card.model_id:
            out["model_id"] = card.model_id
        if card.schedule_job_id:
            out["schedule_job_id"] = card.schedule_job_id
        return out

    # ── Persistence ─────────────────────────────────────────────────────

    def _persist(self) -> None:
        if not self._state_path and not runtime_redis_enabled():
            return
        try:
            data = {
                "paused": self._state.paused,
                "quota_exceeded": self._state.quota_exceeded,
                "total_completed": self._state.total_completed,
                "total_errors": self._state.total_errors,
                "cards": [],
            }
            for card in self._state.cards:
                data["cards"].append({
                    "id": card.id,
                    "task_id": card.task_id,
                    "title": card.title,
                    "priority": card.priority,
                    "skills": card.skills,
                    "assignee": card.assignee,
                    "workdir": card.workdir,
                    "kanban_file": card.kanban_file,
                    "team_id": card.team_id,
                    "model_id": card.model_id,
                    "status": card.status.value,
                    "progress": card.progress[-20:],
                    "last_progress": card.last_progress,
                    "error": card.error,
                    "enqueued_at": card.enqueued_at,
                    "started_at": card.started_at,
                    "finished_at": card.finished_at,
                    "retry_count": card.retry_count,
                    "max_retries": card.max_retries,
                })
            save_json(_PRIORITY_QUEUE_KEY, data, self._state_path)
        except OSError as e:
            log.warning("failed to persist priority queue state: %s", e)

    def _load_state(self) -> None:
        raw = load_json(_PRIORITY_QUEUE_KEY, self._state_path)
        if raw is None:
            return
        try:
            if not isinstance(raw, dict):
                return
            self._state.paused = bool(raw.get("paused", False))
            self._state.quota_exceeded = bool(raw.get("quota_exceeded", False))
            self._state.total_completed = int(raw.get("total_completed", 0))
            self._state.total_errors = int(raw.get("total_errors", 0))
            for row in raw.get("cards") or []:
                if not isinstance(row, dict):
                    continue
                status_str = str(row.get("status") or "queued")
                try:
                    status = CardStatus(status_str)
                except ValueError:
                    status = CardStatus.QUEUED
                # Don't restore running cards as running — re-queue them
                if status == CardStatus.RUNNING:
                    status = CardStatus.QUEUED
                card = PriorityCard(
                    id=str(row.get("id") or uuid.uuid4().hex[:12]),
                    task_id=str(row.get("task_id") or ""),
                    title=str(row.get("title") or ""),
                    priority=int(row.get("priority", 5)),
                    skills=list(row.get("skills") or []),
                    assignee=str(row.get("assignee") or ""),
                    workdir=str(row.get("workdir") or ""),
                    kanban_file=row.get("kanban_file"),
                    team_id=str(row.get("team_id") or ""),
                    model_id=row.get("model_id") or None,
                    status=status,
                    progress=list(row.get("progress") or []),
                    last_progress=str(row.get("last_progress") or ""),
                    error=row.get("error"),
                    enqueued_at=float(row.get("enqueued_at", time.time())),
                    started_at=row.get("started_at"),
                    finished_at=row.get("finished_at"),
                    retry_count=int(row.get("retry_count", 0)),
                    max_retries=int(row.get("max_retries", 3)),
                )
                self._state.cards.append(card)
            self._sort_queue()
            log.info(
                "loaded priority queue: %d cards (%d queued)",
                len(self._state.cards),
                sum(1 for c in self._state.cards if c.status == CardStatus.QUEUED),
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            log.warning("failed to load priority queue state: %s", e)
