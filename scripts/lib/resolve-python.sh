# shellcheck shell=bash
# Resolve which Python binary should run gois (stable path for macOS TCC).
#
# Usage (source this file):
#   source "$(dirname "$0")/lib/resolve-python.sh"
#   PY="$(qclaw_resolve_python "$PROJECT_DIR")"

qclaw_stable_python() {
  echo "${GOIS_PYTHON:-${HOME}/.local/gois/runtime/python3.13}"
}

qclaw_resolve_python() {
  local project_dir="${1:-}"
  local stable
  stable="$(qclaw_stable_python)"
  if [[ -x "$stable" ]]; then
    echo "$stable"
    return 0
  fi
  if [[ -n "$project_dir" && -x "${project_dir}/.venv/bin/python" ]]; then
    echo "${project_dir}/.venv/bin/python"
    return 0
  fi
  return 1
}
