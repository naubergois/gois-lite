#!/usr/bin/env bash
# Bootstrap and start gois in the foreground.
#
# Idempotent — safe to re-run:
#   - initializes git submodules (vendor/hermes-agent, vendor/openclaw)
#   - creates ./.stack/ runtime state directories
#   - creates ./.venv if missing and `pip install -e .` for gois
#   - bootstraps the vendored Hermes venv (and OpenClaw node_modules if npm exists)
#   - ensures ./config.yaml exists (copies from config.example.yaml)
#   - ensures ./.env has DEEPSEEK_API_KEY (falls back to ~/AIManager/.env)
#   - runs `python -m gois --config config.yaml`
#
# Flags:
#   --launchd       install/reload the LaunchAgent (background, starts at login)
#   --kill          stop the LaunchAgent (if any) and any foreground process
#   --tail          tail the launchd stderr log
#   --skip-vendor   skip submodule init + vendor dep install (faster restarts)
#   -h | --help     show this help

set -euo pipefail

PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$PROJECT_DIR"

LABEL="com.naubergois.gois"
DOMAIN="gui/$( id -u )"
VENV="$PROJECT_DIR/.venv"
# shellcheck source=lib/resolve-python.sh
source "${PROJECT_DIR}/scripts/lib/resolve-python.sh"
# shellcheck source=lib/install-editable.sh
source "${PROJECT_DIR}/scripts/lib/install-editable.sh"
PY="$(qclaw_resolve_python "$PROJECT_DIR")"
CONFIG="$PROJECT_DIR/config.yaml"
ENVFILE="$PROJECT_DIR/.env"

c_blue()  { printf "\033[34m%s\033[0m\n" "$*"; }
c_green() { printf "\033[32m%s\033[0m\n" "$*"; }
c_warn()  { printf "\033[33m%s\033[0m\n" "$*"; }
c_err()   { printf "\033[31m%s\033[0m\n" "$*" >&2; }

ensure_venv() {
    if [[ ! -x "$PY" ]]; then
        c_blue "→ creating venv at .venv"
        python3 -m venv .venv
    fi
    c_blue "→ installing deps (pip install -e .)"
    qclaw_install_editable "$PROJECT_DIR"
}

ensure_vendor() {
    # Self-contained: pull Hermes + OpenClaw source via git submodules and
    # populate .stack/ runtime directories. Idempotent.
    if [[ "${SKIP_VENDOR:-0}" -eq 1 ]]; then
        c_blue "→ skipping vendor bootstrap (--skip-vendor)"
        return
    fi
    bash "$PROJECT_DIR/scripts/unify-stack.sh"
}

ensure_config() {
    if [[ ! -f "$CONFIG" ]]; then
        c_blue "→ config.yaml missing; copying from config.example.yaml"
        cp config.example.yaml "$CONFIG"
        c_warn "  edit $CONFIG before serious use (process_pattern, start_command, ...)"
    fi
}

ensure_mongo() {
    if ! "$PY" -c "from gois.mongo import ping; raise SystemExit(0 if ping() else 1)" 2>/dev/null; then
        if [[ -x "$PROJECT_DIR/scripts/setup_mongo.sh" ]]; then
            c_blue "→ MongoDB not reachable; running setup_mongo.sh"
            bash "$PROJECT_DIR/scripts/setup_mongo.sh" || c_warn "  setup_mongo.sh failed"
        else
            c_warn "→ MongoDB not reachable; using config.yaml + SQLite fallbacks"
        fi
    fi
    if "$PY" -c "from gois.mongo import ping; raise SystemExit(0 if ping() else 1)" 2>/dev/null; then
        c_blue "→ MongoDB reachable; running migrate_all_to_mongo --merge"
        if ! "$PY" -m gois.scripts.migrate_all_to_mongo --config "$CONFIG" --merge; then
            c_warn "  migrate_all_to_mongo failed — monitor will fall back to YAML/SQLite seeds"
        fi
    else
        c_warn "→ MongoDB not reachable; using config.yaml + SQLite fallbacks"
    fi
}

ensure_redis() {
    if ! command -v redis-cli >/dev/null 2>&1; then
        c_warn "→ Redis CLI not found; runtime state will use JSON files"
        c_warn "  install: ./scripts/setup_redis.sh"
        return
    fi
    if redis-cli ping >/dev/null 2>&1; then
        c_blue "→ Redis reachable; running migrate_runtime_to_redis"
        if ! "$PY" -m gois.scripts.migrate_runtime_to_redis; then
            c_warn "  migrate_runtime_to_redis failed — using JSON file fallbacks"
        fi
    else
        c_warn "→ Redis not reachable; runtime state will use JSON files"
        c_warn "  start: brew services start redis  (or ./scripts/setup_redis.sh)"
    fi
}

ensure_env() {
    # Create empty .env if missing.
    if [[ ! -f "$ENVFILE" ]]; then
        c_blue "→ .env missing; creating from .env.example"
        cp .env.example "$ENVFILE"
        chmod 600 "$ENVFILE"
    fi
    # Sync DEEPSEEK_* from ~/.hermes, AIManager, CGEClaw, etc. (see secrets_fallback.py)
    if ! "$PY" "$PROJECT_DIR/scripts/sync_deepseek_env.py" 2>/dev/null; then
        if ! grep -q '^DEEPSEEK_API_KEY=sk-' "$ENVFILE" 2>/dev/null; then
            c_warn "  no DEEPSEEK_API_KEY found in sibling projects."
            c_warn "  run: .venv/bin/python scripts/sync_deepseek_env.py"
            c_warn "  or edit $ENVFILE"
        fi
    fi
}

kill_all() {
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
        c_blue "→ bootout $DOMAIN/$LABEL"
        launchctl bootout "$DOMAIN/$LABEL" || true
    fi
    # also kill any foreground process matching the entrypoint
    pkill -f "$PY -m gois" 2>/dev/null && c_blue "→ killed foreground process(es)" || true
    c_green "stopped."
}

install_launchd() {
    rotate_logs
    bash "$PROJECT_DIR/scripts/install-launchd.sh"
}

tail_launchd() {
    c_blue "→ tailing $PROJECT_DIR/gois.stderr.log (ctrl+c to stop)"
    exec tail -f "$PROJECT_DIR/gois.stderr.log" "$PROJECT_DIR/gois.stdout.log"
}

rotate_logs() {
    bash "$PROJECT_DIR/scripts/rotate-logs.sh" || true
}

run_foreground() {
    rotate_logs
    if pgrep -f "$PY -m gois" >/dev/null 2>&1; then
        c_warn "gois is already running in foreground (pid $(pgrep -f "$PY -m gois" | head -1))"
        c_warn "use './scripts/start.sh --kill' to stop, then try again."
        exit 1
    fi
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
        c_warn "a launchd job for $LABEL is already running."
        c_warn "use './scripts/start.sh --kill' first, or '--tail' to follow its logs."
        exit 1
    fi
    c_green "→ starting gois in the foreground (ctrl+c to stop)"
    c_green "  dashboard: http://127.0.0.1:9101/"
    export GOIS_STACK_ROOT="${GOIS_STACK_ROOT:-$PROJECT_DIR/.stack}"
    exec "$PY" -m gois --config "$CONFIG" --env-file "$ENVFILE"
}

usage() {
    grep -E '^# ' "${BASH_SOURCE[0]}" | sed 's/^# //'
}

SKIP_VENDOR=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-vendor)  SKIP_VENDOR=1; shift ;;
        *)              break ;;
    esac
done

case "${1:-}" in
    -h|--help)  usage ;;
    --kill)     kill_all ;;
    --launchd)  ensure_venv && ensure_vendor && ensure_config && ensure_env && ensure_mongo && ensure_redis && install_launchd ;;
    --tail)     tail_launchd ;;
    "")         ensure_venv && ensure_vendor && ensure_config && ensure_env && ensure_mongo && ensure_redis && run_foreground ;;
    *)          c_err "unknown flag: $1"; usage; exit 2 ;;
esac
