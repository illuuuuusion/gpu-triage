#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$REPO_ROOT/app/gpu_diag.py"
REPORT_DIR="$REPO_ROOT/reports"

PYTHON="$(command -v python3 || command -v python || true)"

# 'list' and help only read sysfs through the Python standard library. They must
# not verify or install the multi-GB offline bundle. A help flag counts anywhere
# in the argument list, so 'quick --help' prints usage instead of escalating to
# root and bootstrapping. If no interpreter exists yet, fall through to the
# bootstrap path, which installs one.
NEEDS_RUNTIME=1
case "${1:-}" in
  list | help | --help | -h) NEEDS_RUNTIME=0 ;;
esac
for arg in "$@"; do
  case "$arg" in
    -h | --help) NEEDS_RUNTIME=0 ;;
  esac
done

if [[ $NEEDS_RUNTIME -eq 0 && -n "$PYTHON" ]]; then
  if [[ "${1:-}" == "help" ]]; then
    set -- --help
  fi
  exec "$PYTHON" "$APP" "$@"
fi

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || { echo "Run as root." >&2; exit 2; }
  exec sudo -E "$0" "$@"
fi

mkdir -p "$REPORT_DIR"

bash "$REPO_ROOT/scripts/bootstrap.sh"

# bootstrap.sh may have installed the interpreter that was missing above.
PYTHON="$(command -v python3 || command -v python || true)"
[[ -n "$PYTHON" ]] || { echo "No Python interpreter after bootstrap." >&2; exit 2; }

export GPU_TRIAGE_REPORT_DIR="$REPORT_DIR"

exec "$PYTHON" "$APP" "$@"
