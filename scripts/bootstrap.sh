#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG_DIR="$REPO_ROOT/offline/packages"
MANIFEST="$REPO_ROOT/offline/manifest.env"
SUMS="$REPO_ROOT/offline/SHA256SUMS"
PROFILE_SUMS="$REPO_ROOT/offline/PROFILE-SHA256SUMS"

PROFILE="safe-runtime"
if [[ "${1:-}" == "--profile" ]]; then
  [[ $# -eq 2 ]] || { echo "Usage: $0 [--profile safe-runtime|driver-bound-runtime]" >&2; exit 2; }
  PROFILE="$2"
elif [[ $# -ne 0 ]]; then
  echo "Usage: $0 [--profile safe-runtime|driver-bound-runtime]" >&2
  exit 2
fi
case "$PROFILE" in
  safe-runtime | driver-bound-runtime) ;;
  *) echo "Unknown runtime profile: $PROFILE" >&2; exit 2 ;;
esac
PROFILE_FILE="$REPO_ROOT/offline/profiles/$PROFILE.files"

# /run is tmpfs, so the stamp lives for exactly one live boot and is never
# written back to the USB stick.
STAMP_DIR="/run/gpu-triage"
STAMP="$STAMP_DIR/bootstrap.ok"

log() { printf '[bootstrap] %s\n' "$*"; }
die() { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 2; }

# Identify the exact bundle that was installed. Swapping the stick, remounting
# elsewhere or rebuilding the bundle changes this value and forces a re-run.
bundle_id() {
  {
    printf 'kernel=%s\n' "$(uname -r)"
    printf 'root=%s\n' "$REPO_ROOT"
    if [[ -f "$MANIFEST" ]]; then sha256sum < "$MANIFEST"; else printf 'no-manifest\n'; fi
    if [[ -f "$SUMS" ]]; then sha256sum < "$SUMS"; else printf 'no-sums\n'; fi
    if [[ -n "${PROFILE_SUMS:-}" && -f "$PROFILE_SUMS" ]]; then sha256sum < "$PROFILE_SUMS"; else printf 'no-profile-sums\n'; fi
    printf 'profile=%s\n' "${PROFILE:-safe-runtime}"
    if [[ -n "${PROFILE_FILE:-}" && -f "$PROFILE_FILE" ]]; then sha256sum < "$PROFILE_FILE"; else printf 'no-profile\n'; fi
  } | sha256sum | awk '{print $1}'
}

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || die "Run as root (the Arch ISO normally starts a root shell)."
  exec sudo -E "$0" "$@"
fi

[[ -f /etc/arch-release ]] || die "This MVP expects the official Arch Linux live environment."
[[ -d "$PKG_DIR" ]] || die "Offline package directory missing: $PKG_DIR"
[[ -f "$SUMS" ]] || die "SHA256SUMS missing; refusing an unverified offline installation."
LEGACY_ALL_HASHED=0
if [[ ! -f "$PROFILE_SUMS" || ! -f "$PROFILE_FILE" ]]; then
  if [[ "$PROFILE" == "driver-bound-runtime" && ! -e "$PROFILE_SUMS" && ! -e "$REPO_ROOT/offline/profiles" ]]; then
    # The pinned 2026.08.01 release predates role metadata. Its hash-covered
    # package set is exactly the old union runtime. That union is acceptable
    # only for an already-bound Stage-3 run; safe-runtime must still fail closed
    # rather than install GPU/Vulkan packages.
    LEGACY_ALL_HASHED=1
  else
    die "Runtime profile metadata is missing or incomplete. Rebuild the bundle; safe-runtime never falls back to the union package set."
  fi
fi
mapfile -t DISCOVERED_FILES < <(find "$PKG_DIR" -maxdepth 1 -type f -name '*.pkg.tar.zst' -print | sort)
[[ ${#DISCOVERED_FILES[@]} -gt 0 ]] || die "Offline bundle is empty. Build it on the Internet-connected PC first."

BUNDLE_ID="$(bundle_id)"
if [[ "${GPU_TRIAGE_FORCE_BOOTSTRAP:-0}" != "1" && -f "$STAMP" && "$(cat "$STAMP" 2>/dev/null)" == "$BUNDLE_ID" ]]; then
  log "Runtime already prepared for this bundle in this boot; skipping verification and installation."
  log "Set GPU_TRIAGE_FORCE_BOOTSTRAP=1 to redo it."
  exit 0
fi

EXPECTED_KERNEL=""
ARCHISO_DATE=""
BUNDLE_CREATED=""
if [[ -f "$MANIFEST" ]]; then
  # Parse the small allowlist as data.  A removable medium must never gain shell
  # execution merely because a manifest line was modified.
  while IFS= read -r line; do
    if [[ "$line" =~ ^(EXPECTED_KERNEL|ARCHISO_DATE|BUNDLE_CREATED|ISO_SUBTRACT)=\'(.*)\'$ ]]; then
      case "${BASH_REMATCH[1]}" in
        EXPECTED_KERNEL) EXPECTED_KERNEL="${BASH_REMATCH[2]}" ;;
        ARCHISO_DATE) ARCHISO_DATE="${BASH_REMATCH[2]}" ;;
        BUNDLE_CREATED) BUNDLE_CREATED="${BASH_REMATCH[2]}" ;;
        ISO_SUBTRACT) ISO_SUBTRACT="${BASH_REMATCH[2]}" ;;
      esac
    fi
  done < "$MANIFEST"
fi

RUNNING_KERNEL="$(uname -r)"
if [[ -n "${EXPECTED_KERNEL:-}" && "$RUNNING_KERNEL" != "$EXPECTED_KERNEL" ]]; then
  cat >&2 <<MSG
[bootstrap] ERROR: Runtime bundle / Arch ISO mismatch.
  Running ISO kernel: $RUNNING_KERNEL
  Bundle expects:      $EXPECTED_KERNEL
  Bundle ISO date:     ${ARCHISO_DATE:-unknown}

Use the Arch ISO whose date was used when building this offline bundle,
or rebuild the bundle for the ISO currently on the USB stick.
MSG
  exit 3
fi

log "Verifying offline bundle and runtime-profile hashes..."
(cd "$REPO_ROOT/offline" && sha256sum -c SHA256SUMS --quiet) || die "Offline package checksum verification failed."
if [[ $LEGACY_ALL_HASHED -eq 0 ]]; then
  (cd "$REPO_ROOT/offline" && sha256sum -c PROFILE-SHA256SUMS --quiet) || die "Runtime profile checksum verification failed."
else
  log "Legacy driver-bound bundle: using the complete SHA256-covered union runtime."
fi

declare -A HASHED=()
while read -r _ relative; do
  relative="${relative#\*}"
  [[ "$relative" == packages/*.pkg.tar.zst ]] || die "Invalid package path in SHA256SUMS: $relative"
  HASHED["$relative"]=1
done < "$SUMS"

PACKAGE_FILES=()
if [[ $LEGACY_ALL_HASHED -eq 1 ]]; then
  for relative in "${!HASHED[@]}"; do
    [[ -f "$REPO_ROOT/offline/$relative" ]] || die "Hashed package is missing: $relative"
    PACKAGE_FILES+=("$REPO_ROOT/offline/$relative")
  done
else
  while IFS= read -r relative; do
    [[ -n "$relative" && "$relative" != \#* ]] || continue
    [[ "$relative" == packages/*.pkg.tar.zst && "$relative" != *..* ]] \
      || die "Invalid path in $PROFILE_FILE: $relative"
    [[ -n "${HASHED[$relative]:-}" ]] || die "Profile package is not covered by SHA256SUMS: $relative"
    [[ -f "$REPO_ROOT/offline/$relative" ]] || die "Profile package is missing: $relative"
    PACKAGE_FILES+=("$REPO_ROOT/offline/$relative")
  done < "$PROFILE_FILE"
fi

if [[ ${#PACKAGE_FILES[@]} -eq 0 ]]; then
  log "Profile $PROFILE is already fully provided by the pinned Arch ISO; no package install is needed."
else
  log "Profile $PROFILE selects ${#PACKAGE_FILES[@]} verified package file(s)."
fi

log "Installing required runtime packages from USB only..."
# --needed avoids rewriting packages that already exist at the identical version
# in the live ISO.
if [[ ${#PACKAGE_FILES[@]} -gt 0 ]] && ! pacman -U --needed --noconfirm "${PACKAGE_FILES[@]}"; then
  if [[ "${ISO_SUBTRACT:-0}" == "1" ]]; then
    die "Installation failed.
This bundle ships only what the Arch ISO of ${ARCHISO_DATE:-its build date} does not
already provide, so it can only be installed from that live environment. Missing
dependencies mean this is not that ISO. Boot the matching ISO, or rebuild with
build_bundle.sh --no-iso-subtract for a self-contained bundle."
  fi
  die "Installation of the offline packages failed."
fi

mkdir -p "$STAMP_DIR"
printf '%s' "$BUNDLE_ID" > "$STAMP"

log "Runtime profile $PROFILE ready. No GPU module or binding action was performed."
log "Kernel: $RUNNING_KERNEL"
log "Bundle: ${BUNDLE_CREATED:-unknown build time}"
