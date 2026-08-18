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
RUNTIME_PROFILE="safe-runtime"
HELP_REQUESTED=0
case "${1:-}" in
  list | doctor | help | --help | -h) NEEDS_RUNTIME=0 ;;
esac
for arg in "$@"; do
  case "$arg" in
    -h | --help) NEEDS_RUNTIME=0; HELP_REQUESTED=1 ;;
  esac
done

# The pinned Arch ISO normally already provides the complete safe profile.
# Reuse it as-is; only a missing Python/lspci tool justifies an offline install.
case "${1:-}" in
  triage | quick)
    if [[ -n "$PYTHON" ]] && command -v lspci >/dev/null 2>&1; then
      NEEDS_RUNTIME=0
    fi

    # A driver-bound profile is prepared only when the explicitly selected
    # target is already on its vendor-expected driver. This read-only routing
    # prevents safe/unbound preflight runs from installing Vulkan or GPU-driver
    # packages, and it never loads or binds a module.
    PREFLIGHT_ONLY=0
    GPU_BDF=""
    EXPECT_GPU=0
    for arg in "$@"; do
      if [[ $EXPECT_GPU -eq 1 ]]; then
        GPU_BDF="$arg"
        EXPECT_GPU=0
        continue
      fi
      case "$arg" in
        --preflight-only) PREFLIGHT_ONLY=1 ;;
        --gpu) EXPECT_GPU=1 ;;
        --gpu=*) GPU_BDF="${arg#--gpu=}" ;;
      esac
    done
    if [[ $HELP_REQUESTED -eq 0 && $PREFLIGHT_ONLY -eq 0 && "$GPU_BDF" =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$ ]]; then
      DEVICE_ROOT="/sys/bus/pci/devices/${GPU_BDF,,}"
      if [[ -r "$DEVICE_ROOT/vendor" && -L "$DEVICE_ROOT/driver" ]]; then
        VENDOR_ID="$(<"$DEVICE_ROOT/vendor")"
        OBSERVED_DRIVER="$(basename "$(readlink -f "$DEVICE_ROOT/driver")")"
        if [[ ( "$VENDOR_ID" == "0x1002" && "$OBSERVED_DRIVER" == "amdgpu" ) ||
              ( "$VENDOR_ID" == "0x10de" && "$OBSERVED_DRIVER" == "nvidia" ) ]]; then
          RUNTIME_PROFILE="driver-bound-runtime"
          NEEDS_RUNTIME=1
        fi
      fi
    fi
    ;;
esac

if [[ $NEEDS_RUNTIME -eq 0 && -n "$PYTHON" ]]; then
  if [[ "${1:-}" == "help" ]]; then
    set -- --help
  fi
  exec "$PYTHON" "$APP" "$@"
fi

if [[ "${1:-}" == "doctor" && -z "$PYTHON" ]]; then
  echo "No Python interpreter; doctor does not install or change the live system." >&2
  exit 2
fi

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || { echo "Run as root." >&2; exit 2; }
  exec sudo -E "$0" "$@"
fi

mkdir -p "$REPORT_DIR"

# The profile was selected read-only from the requested mode and the already
# observed binding. The installer never loads, removes, binds, or unbinds a GPU
# driver.
bash "$REPO_ROOT/scripts/bootstrap.sh" --profile "$RUNTIME_PROFILE"

# bootstrap.sh may have installed the interpreter that was missing above.
PYTHON="$(command -v python3 || command -v python || true)"
[[ -n "$PYTHON" ]] || { echo "No Python interpreter after bootstrap." >&2; exit 2; }

export GPU_TRIAGE_REPORT_DIR="$REPORT_DIR"

exec "$PYTHON" "$APP" "$@"
