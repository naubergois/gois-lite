# shellcheck shell=bash
# Install gois in editable mode (pip, uv, or python -m pip).
#
# Usage:
#   source "$(dirname "$0")/lib/install-editable.sh"
#   qclaw_install_editable "$PROJECT_DIR"

qclaw_install_editable() {
  local project_dir="${1:?project_dir required}"
  local venv="${project_dir}/.venv"
  local py="${venv}/bin/python"
  local pip="${venv}/bin/pip"

  if [[ -x "$pip" ]]; then
    "$pip" install -q --upgrade pip >/dev/null 2>&1 || true
    "$pip" install -q -e "${project_dir}[dev]"
    return 0
  fi
  if command -v uv >/dev/null 2>&1; then
    uv pip install -q -e "${project_dir}[dev]"
    return 0
  fi
  if [[ -x "$py" ]] && "$py" -m pip --version >/dev/null 2>&1; then
    "$py" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
    "$py" -m pip install -q -e "${project_dir}[dev]"
    return 0
  fi
  echo "ERROR: no pip/uv found to install ${project_dir}" >&2
  return 1
}
