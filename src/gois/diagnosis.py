"""Build a human-readable QClaw down diagnosis from monitor status snapshots."""

from __future__ import annotations

import json
from typing import Any, Optional


def _parse_failure_summary(raw: Optional[str]) -> Optional[dict[str, Any]]:
    if not raw or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _check_label(name: str, check: dict[str, Any]) -> tuple[str, str]:
    """Return (short label, detail) for one health check."""
    if name == "process":
        pattern = check.get("pattern") or "?"
        pids = check.get("pids") or []
        if check.get("ok"):
            return (
                "Processo",
                f"ativo (PID {', '.join(str(p) for p in pids) or '—'})",
            )
        return (
            "Processo",
            f"não encontrado (pgrep -f {pattern})",
        )

    if name == "http":
        url = check.get("url") or "?"
        if check.get("ok"):
            status = check.get("status")
            return ("HTTP", f"{url} → {status}")
        err = check.get("error") or ""
        status = check.get("status")
        if status is not None:
            return (
                "HTTP",
                f"{url} respondeu {status} (esperado outro código)",
            )
        if "ConnectError" in err or "ConnectTimeout" in err:
            return (
                "HTTP",
                f"sem conexão em {url} — {err}",
            )
        if "ReadTimeout" in err or "Timeout" in err:
            return (
                "HTTP",
                f"timeout em {url} — {err}",
            )
        return ("HTTP", f"falha em {url}" + (f" — {err}" if err else ""))

    if name == "responsive":
        app = check.get("app") or "QClaw"
        if check.get("skipped"):
            reason = check.get("reason") or "probe ignorado"
            return ("Interface", reason[:200])
        if check.get("hang"):
            return (
                "Interface",
                f"{app} não responde (probe AppleScript expirou)",
            )
        reason = check.get("reason") or ""
        if check.get("ok"):
            windows = check.get("windows")
            extra = f", {windows} janela(s)" if windows else ""
            return ("Interface", f"{app} responsivo{extra}")
        if "not running" in reason.lower() or "não" in reason.lower():
            return ("Interface", f"{app} não está em execução")
        return ("Interface", reason[:200] or "falha no probe de interface")

    ok = check.get("ok")
    return (name, "ok" if ok else "falhou")


def _process_tree_notes(procs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Infer crash mode from the live process tree."""
    notes: list[dict[str, str]] = []
    if not procs:
        notes.append(
            {
                "id": "no_procs",
                "label": "Nenhum processo QClaw/OpenClaw detectado no host",
                "detail": "O app pode ter encerrado por completo ou ainda não subiu.",
            }
        )
        return notes

    roles = {str(p.get("role") or "") for p in procs}
    has_main = "main" in roles
    has_gateway = "gateway" in roles
    helpers = [p for p in procs if p.get("role") == "helper"]
    mains = [p for p in procs if p.get("role") == "main"]

    if helpers and not has_main:
        notes.append(
            {
                "id": "orphan_helpers",
                "label": "Helpers do Electron ainda rodando, mas o processo principal sumiu",
                "detail": (
                    f"{len(helpers)} helper(s) sem QClaw (main) — típico de crash do app "
                    "com filhos órfãos."
                ),
            }
        )
    elif has_main and not has_gateway:
        notes.append(
            {
                "id": "no_gateway",
                "label": "QClaw aberto, mas o openclaw-gateway não aparece na árvore de processos",
                "detail": (
                    "O health check HTTP costuma falhar quando o gateway não está ativo."
                ),
            }
        )
    elif has_main and has_gateway and len(mains) == 1:
        main = mains[0]
        notes.append(
            {
                "id": "partial_up",
                "label": "Processo principal e gateway presentes",
                "detail": (
                    f"PID main={main.get('pid')} — a falha provavelmente é HTTP, "
                    "interface travada ou erro interno do gateway."
                ),
            }
        )
    return notes


def _log_scanner_notes(scanner: Optional[dict[str, Any]]) -> list[dict[str, str]]:
    if not scanner or not scanner.get("enabled"):
        return []
    details = scanner.get("last_details") or []
    if not isinstance(details, list) or not details:
        return []
    lines: list[str] = []
    for item in details[:3]:
        if not isinstance(item, dict):
            continue
        pat = item.get("pattern") or "?"
        file = item.get("file") or "?"
        line = (item.get("line") or "")[:120]
        lines.append(f"{pat} em {file}: {line}")
    if not lines:
        return []
    return [
        {
            "id": "log_matches",
            "label": "Padrões de erro recentes nos logs",
            "detail": " | ".join(lines),
        }
    ]


def _reaper_notes(reaper: Optional[dict[str, Any]]) -> list[dict[str, str]]:
    if not reaper:
        return []
    killed = int(reaper.get("last_killed") or 0)
    if killed <= 0:
        return []
    summary = str(reaper.get("last_summary") or "").strip()
    return [
        {
            "id": "reaper",
            "label": f"Reaper eliminou {killed} processo(s) órfão(s) recentemente",
            "detail": summary or "Pode indicar que o processo principal caiu antes.",
        }
    ]


def _recovery_notes(snap: dict[str, Any]) -> list[str]:
    hints: list[str] = []
    attempts = int(snap.get("recovery_attempts_this_run") or 0)
    max_attempts = int(snap.get("max_recovery_attempts") or 0)
    if attempts > 0:
        hints.append(
            f"Recuperação automática já tentou {attempts}"
            + (f"/{max_attempts}" if max_attempts else "")
            + " vez(es) nesta execução do monitor."
        )
    report = (snap.get("last_recovery_report") or "").strip()
    if report:
        preview = report.replace("\n", " ")[:220]
        hints.append(f"Último relatório de recovery: {preview}")

    doctor = snap.get("openclaw_doctor") or {}
    if isinstance(doctor, dict) and doctor.get("last_summary"):
        trig = doctor.get("last_trigger") or ""
        summ = str(doctor["last_summary"])[:180]
        hints.append(
            f"openclaw doctor"
            + (f" ({trig})" if trig else "")
            + f": {summ}"
        )
    return hints


def build_qclaw_diagnosis(snap: dict[str, Any]) -> dict[str, Any]:
    """Return structured diagnosis for dashboard when QClaw is not healthy."""
    failure = _parse_failure_summary(snap.get("last_failure_summary"))
    checks_raw: dict[str, Any] = {}
    if failure:
        raw_checks = failure.get("checks")
        if isinstance(raw_checks, dict):
            checks_raw = raw_checks

    check_rows: list[dict[str, str]] = []
    failed_checks: list[str] = []
    for name, check in checks_raw.items():
        if not isinstance(check, dict):
            continue
        label, detail = _check_label(name, check)
        check_rows.append({"name": name, "label": label, "detail": detail, "ok": bool(check.get("ok"))})
        if not check.get("ok"):
            failed_checks.append(name)

    causes: list[dict[str, str]] = []
    headline = "QClaw indisponível"
    likely = ""

    if "process" in failed_checks:
        headline = "Processo QClaw ausente"
        likely = (
            "O executável principal não está rodando — o app pode ter fechado, "
            "crashado ou não foi iniciado."
        )
        causes.append(
            {
                "id": "process_down",
                "label": "Processo principal não encontrado",
                "detail": next(
                    (r["detail"] for r in check_rows if r["name"] == "process"),
                    "",
                ),
            }
        )
    elif "http" in failed_checks and "process" not in failed_checks:
        http_check = checks_raw.get("http") or {}
        err = str(http_check.get("error") or "")
        if "Connect" in err:
            headline = "Gateway HTTP inacessível"
            likely = (
                "O QClaw parece estar rodando, mas o endpoint de saúde não aceita conexão — "
                "o openclaw-gateway pode não ter subido ou está escutando em outra porta."
            )
        elif http_check.get("status") is not None:
            headline = f"Gateway HTTP retornou {http_check.get('status')}"
            likely = "O gateway respondeu, mas com código HTTP inesperado."
        else:
            headline = "Falha no health check HTTP"
            likely = "O probe HTTP do monitor falhou."
        causes.append(
            {
                "id": "http_down",
                "label": "Health URL sem resposta válida",
                "detail": next(
                    (r["detail"] for r in check_rows if r["name"] == "http"),
                    "",
                ),
            }
        )
    elif "responsive" in failed_checks:
        resp = checks_raw.get("responsive") or {}
        if resp.get("hang"):
            headline = "Interface QClaw travada"
            likely = (
                "O processo existe, mas a UI não responde ao probe — possível deadlock "
                "ou janela congelada."
            )
        else:
            headline = "Interface QClaw não responsiva"
            likely = resp.get("reason") or "Falha no probe de interface gráfica."
        causes.append(
            {
                "id": "ui_down",
                "label": "Probe de interface falhou",
                "detail": next(
                    (r["detail"] for r in check_rows if r["name"] == "responsive"),
                    "",
                ),
            }
        )

    if not causes and not failure:
        likely = (
            "Sem resumo de falha recente — o monitor pode estar iniciando ou "
            "ainda não registrou o primeiro health check."
        )

    procs = snap.get("qclaw_processes") or []
    if isinstance(procs, list):
        causes.extend(_process_tree_notes(procs))
    causes.extend(_log_scanner_notes(snap.get("log_scanner")))
    causes.extend(_reaper_notes(snap.get("reaper")))

    hints: list[str] = []
    if "process" in failed_checks or not procs:
        hints.append("Abra o QClaw manualmente ou execute o comando de start configurado no monitor.")
    if "http" in failed_checks:
        hints.append(
            "Confira a porta do gateway em ~/.qclaw-oversea/qclaw.json e se openclaw-gateway está ativo."
        )
        hints.append("Veja os logs do gateway em log_paths do config (seção log scanner).")
    if "responsive" in failed_checks:
        hints.append("Force quit do QClaw e reinicie; em macOS, conceda permissão de Automação ao monitor se o probe foi ignorado.")
    if not hints:
        hints.append("Consulte «last failure summary» e «log scanner» abaixo para detalhes técnicos.")
    hints.extend(_recovery_notes(snap))

    fails = int(snap.get("consecutive_failures") or 0)
    threshold = int(snap.get("failure_threshold") or 0)

    return {
        "headline": headline,
        "likely_cause": likely,
        "causes": causes,
        "hints": hints[:6],
        "checks": check_rows,
        "consecutive_failures": fails,
        "failure_threshold": threshold,
        "last_health_fail_ts": snap.get("last_health_fail_ts"),
        "last_health_ok_ts": snap.get("last_health_ok_ts"),
    }
