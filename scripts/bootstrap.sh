#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG_DIR="$REPO_ROOT/offline/packages"
MANIFEST="$REPO_ROOT/offline/manifest.env"
SUMS="$REPO_ROOT/offline/SHA256SUMS"

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
  } | sha256sum | awk '{print $1}'
}

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || die "Run as root (the Arch ISO normally starts a root shell)."
  exec sudo -E "$0" "$@"
fi

[[ -f /etc/arch-release ]] || die "This MVP expects the official Arch Linux live environment."
[[ -d "$PKG_DIR" ]] || die "Offline package directory missing: $PKG_DIR"
mapfile -t PACKAGE_FILES < <(find "$PKG_DIR" -maxdepth 1 -type f -name '*.pkg.tar.zst' -print | sort)
[[ ${#PACKAGE_FILES[@]} -gt 0 ]] || die "Offline bundle is empty. Build it on the Internet-connected PC first."

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
  # Generated only by our own build script; shell-format values are quoted.
  # shellcheck disable=SC1090
  source "$MANIFEST"
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

if [[ -f "$SUMS" ]]; then
  log "Verifying offline bundle hashes..."
  (cd "$REPO_ROOT/offline" && sha256sum -c SHA256SUMS --quiet) || die "Offline package checksum verification failed."

  # Install exactly what was hashed. A package file sitting in the directory but
  # absent from SHA256SUMS was never verified and must not enter the transaction.
  DISCOVERED=${#PACKAGE_FILES[@]}
  mapfile -t PACKAGE_FILES < <(
    awk -v dir="$REPO_ROOT/offline" 'NF >= 2 { sub(/^\*/, "", $2); print dir "/" $2 }' "$SUMS" | sort
  )
  [[ ${#PACKAGE_FILES[@]} -gt 0 ]] || die "SHA256SUMS lists no packages."
  if [[ ${#PACKAGE_FILES[@]} -lt $DISCOVERED ]]; then
    log "Ignoring $((DISCOVERED - ${#PACKAGE_FILES[@]})) package file(s) not covered by SHA256SUMS."
  fi
else
  log "No SHA256SUMS present; installing unverified package files from $PKG_DIR."
fi

log "Installing required runtime packages from USB only..."
# --needed avoids rewriting packages that already exist at the identical version
# in the live ISO.
if ! pacman -U --needed --noconfirm "${PACKAGE_FILES[@]}"; then
  if [[ "${ISO_SUBTRACT:-0}" == "1" ]]; then
    die "Installation failed.
This bundle ships only what the Arch ISO of ${ARCHISO_DATE:-its build date} does not
already provide, so it can only be installed from that live environment. Missing
dependencies mean this is not that ISO. Boot the matching ISO, or rebuild with
build_bundle.sh --no-iso-subtract for a self-contained bundle."
  fi
  die "Installation of the offline packages failed."
fi

log "Refreshing module dependency metadata..."
depmod -a "$RUNNING_KERNEL" || true

# AMD is normally already bound automatically. Loading is harmless if no AMD dGPU exists.
modprobe amdgpu 2>/dev/null || true

# The official ISO may have bound nouveau before the NVIDIA package was installed.
# Because the diagnostic display is expected to run from an iGPU, detach only
# non-boot NVIDIA display devices from nouveau before loading NVIDIA's module.
for dev in /sys/bus/pci/devices/*; do
  [[ -r "$dev/vendor" && -r "$dev/class" ]] || continue
  [[ "$(<"$dev/vendor")" == "0x10de" ]] || continue
  cls="$(<"$dev/class")"
  [[ "$cls" == 0x03* ]] || continue
  [[ -r "$dev/boot_vga" && "$(<"$dev/boot_vga")" == "1" ]] && continue
  if [[ -L "$dev/driver" && "$(basename "$(readlink -f "$dev/driver")")" == "nouveau" ]]; then
    bdf="$(basename "$dev")"
    log "Detaching test GPU $bdf from nouveau..."
    printf '%s' "$bdf" > /sys/bus/pci/drivers/nouveau/unbind || true
  fi
done

modprobe -r nouveau 2>/dev/null || true
modprobe nvidia 2>/dev/null || true
modprobe nvidia_uvm 2>/dev/null || true
modprobe nvidia_drm 2>/dev/null || true

mkdir -p "$STAMP_DIR"
printf '%s' "$BUNDLE_ID" > "$STAMP"

log "Runtime ready."
log "Kernel: $RUNNING_KERNEL"
log "Bundle: ${BUNDLE_CREATED:-unknown build time}"
