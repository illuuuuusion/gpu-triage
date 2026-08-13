#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$REPO_ROOT/app/gpu_diag.py"
REPORT_DIR="$REPO_ROOT/reports"

PYTHON="$(command -v python3 || command -v python || true)"

# 'list', 'selftest' and help only read sysfs through the Python standard
# library. They must not verify or install the multi-GB offline bundle.
# If no interpreter exists yet, fall through to the bootstrap path, which
# installs one.
case "${1:-}" in
  list | selftest | help | --help | -h)
    if [[ -n "$PYTHON" ]]; then
      if [[ "${1:-}" == "help" ]]; then
        set -- --help
      fi
      export GPU_TRIAGE_REPO_ROOT="$REPO_ROOT"
      exec "$PYTHON" "$APP" "$@"
    fi
    ;;
esac

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || { echo "Run as root." >&2; exit 2; }
  exec sudo -E "$0" "$@"
fi

mkdir -p "$REPORT_DIR"

bash "$REPO_ROOT/scripts/bootstrap.sh"

# bootstrap.sh may have installed the interpreter that was missing above.
PYTHON="$(command -v python3 || command -v python || true)"
[[ -n "$PYTHON" ]] || { echo "No Python interpreter after bootstrap." >&2; exit 2; }

export GPU_TRIAGE_REPO_ROOT="$REPO_ROOT"
export GPU_TRIAGE_REPORT_DIR="$REPORT_DIR"

exec "$PYTHON" "$APP" "$@"
