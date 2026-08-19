#!/usr/bin/env bash
set -euo pipefail

# Build the offline package set for a specific official Arch ISO date.
#
# Usage:
#   ./offline/build_bundle.sh [options] /path/to/archlinux-2026.08.01-x86_64.iso
#
# Options:
#   --no-iso-subtract   ship the full dependency closure, including packages the
#                       live ISO already provides
#   --clean-cache       discard the persistent download cache before building
#   --keep-cache-only   do not write offline/dist/*.zip
#
# Environment:
#   GPU_TRIAGE_OWNER    uid:gid to hand the generated files to (default: the
#                       user behind sudo). Useful when building in a container.
#
# The script resolves the dependency closure against the Arch Linux Archive
# snapshot matching the ISO filename date and downloads it with pacman. There is
# no pacstrap and no chroot, so it runs in a plain unprivileged container image
# and in CI. pacman itself still insists on uid 0 for -Sy/-Sw, so the script
# re-executes under sudo — but every pacman path (database, package cache, log,
# install root) is redirected into this repository, so the build never reads or
# modifies the host system's package state.
#
# What ends up on the USB stick is the closure minus everything the ISO already
# has installed, read from the ISO's own arch/pkglist.x86_64.txt. The diagnostic
# PC installs only from those files and never contacts a mirror.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/offline/bundle_helpers.sh"
PKG_DIR="$REPO_ROOT/offline/packages"
DL_CACHE="$REPO_ROOT/offline/.dlcache"
DIST_DIR="$REPO_ROOT/offline/dist"
LIST_FILE="$REPO_ROOT/offline/package-list.txt"
SAFE_LIST_FILE="$REPO_ROOT/offline/package-list-safe.txt"
DRIVER_LIST_FILE="$REPO_ROOT/offline/package-list-driver-bound.txt"
MANIFEST="$REPO_ROOT/offline/manifest.env"
EXCLUDED="$REPO_ROOT/offline/excluded.txt"
PROFILES_DIR="$REPO_ROOT/offline/profiles"
PROFILE_SUMS="$REPO_ROOT/offline/PROFILE-SHA256SUMS"
HELPER_DIR="$REPO_ROOT/offline/helper"
HELPER_SUMS="$REPO_ROOT/offline/HELPER-SHA256SUMS"

ISO_PATH=""
ISO_SUBTRACT=1
CLEAN_CACHE=0
WRITE_DIST=1
ALL_ARGS=("$@")

log() { printf '[bundle] %s\n' "$*"; }
warn() { printf '[bundle] WARNING: %s\n' "$*" >&2; }
die() { printf '[bundle] ERROR: %s\n' "$*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-iso-subtract) ISO_SUBTRACT=0 ;;
    --clean-cache) CLEAN_CACHE=1 ;;
    --keep-cache-only) WRITE_DIST=0 ;;
    -h | --help)
      sed -n '/^# Build the offline/,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    -*) die "Unknown option: $1" ;;
    *)
      [[ -z "$ISO_PATH" ]] || die "Expected exactly one ISO path"
      ISO_PATH="$1"
      ;;
  esac
  shift
done

[[ -n "$ISO_PATH" ]] || die "Usage: $0 [options] /path/to/archlinux-YYYY.MM.DD-x86_64.iso"
[[ -f "$ISO_PATH" ]] || die "ISO not found: $ISO_PATH"
[[ -f "$LIST_FILE" ]] || die "Package list missing: $LIST_FILE"
[[ -f "$SAFE_LIST_FILE" ]] || die "Safe runtime package list missing: $SAFE_LIST_FILE"
[[ -f "$DRIVER_LIST_FILE" ]] || die "Driver-bound runtime package list missing: $DRIVER_LIST_FILE"

ISO_NAME="$(basename "$ISO_PATH")"
if [[ "$ISO_NAME" =~ archlinux-([0-9]{4})\.([0-9]{2})\.([0-9]{2})-x86_64\.iso$ ]]; then
  YEAR="${BASH_REMATCH[1]}"; MONTH="${BASH_REMATCH[2]}"; DAY="${BASH_REMATCH[3]}"
else
  die "Expected an official ISO filename like archlinux-2026.08.01-x86_64.iso"
fi
ARCHISO_DATE="$YEAR.$MONTH.$DAY"
ARCHIVE_DATE="$YEAR/$MONTH/$DAY"

# pacman refuses -Sy and -Sw for non-root regardless of where its paths point,
# so escalate after the arguments have been validated. Nothing below writes
# outside this repository and the temporary directory.
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || die "pacman requires root for -Sy/-Sw; run this as root."
  exec sudo -E "$0" "${ALL_ARGS[@]}"
fi

command -v pacman >/dev/null 2>&1 || die "Run this on Arch Linux (or in an archlinux container)."
command -v bsdtar >/dev/null 2>&1 || die "bsdtar is required (package: libarchive)."
command -v make >/dev/null 2>&1 || die "make is required to build the native Phase-4 helper."
command -v cc >/dev/null 2>&1 || die "a C17 compiler is required to build the native Phase-4 helper."
command -v glslangValidator >/dev/null 2>&1 || die "glslangValidator is required for reproducible SPIR-V."

mapfile -t TARGETS < <(grep -Ev '^\s*(#|$)' "$LIST_FILE")
mapfile -t SAFE_TARGETS < <(grep -Ev '^\s*(#|$)' "$SAFE_LIST_FILE")
mapfile -t DRIVER_TARGETS < <(grep -Ev '^\s*(#|$)' "$DRIVER_LIST_FILE")
[[ ${#TARGETS[@]} -gt 0 ]] || die "Package list is empty"
[[ ${#SAFE_TARGETS[@]} -gt 0 ]] || die "Safe runtime package list is empty"
[[ ${#DRIVER_TARGETS[@]} -gt 0 ]] || die "Driver-bound runtime package list is empty"

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

[[ $CLEAN_CACHE -eq 1 ]] && { log "Discarding download cache."; rm -rf "$DL_CACHE"; }
mkdir -p "$PKG_DIR" "$DL_CACHE" "$TMP/db/local" "$TMP/root"
find "$PKG_DIR" -maxdepth 1 -type f ! -name .gitkeep -delete

# An empty --dbpath means pacman sees an empty local database and therefore
# resolves the *complete* closure instead of skipping what the build host
# happens to have installed. --logfile and --cachedir keep the build out of
# /var, which is what makes the unprivileged run possible.
PACCONF="$TMP/pacman.conf"
cat > "$PACCONF" <<CONF
[options]
Architecture = x86_64
ParallelDownloads = 5
SigLevel = Required DatabaseOptional
LocalFileSigLevel = Optional

[core]
Server = https://archive.archlinux.org/repos/$ARCHIVE_DATE/\$repo/os/\$arch

[extra]
Server = https://archive.archlinux.org/repos/$ARCHIVE_DATE/\$repo/os/\$arch
CONF

pac() {
  pacman --config "$PACCONF" --dbpath "$TMP/db" --root "$TMP/root" \
         --cachedir "$DL_CACHE" --logfile "$TMP/pacman.log" "$@"
}

log "Building bundle against Arch archive snapshot $ARCHISO_DATE..."

log "Syncing package databases from the archive snapshot..."
pac -Sy --noconfirm >/dev/null || die "Could not sync the archive snapshot for $ARCHISO_DATE."

# base + linux model the live environment fundamentals; the package list adds the
# GPU runtime on top. Nearly all of base/linux is subtracted again below — it is
# resolved so that the closure is complete before anything is removed from it.
ALL_TARGETS=(base linux "${TARGETS[@]}")

log "Resolving dependency closure..."
if ! pac -Sp --print-format '%r %n %v %l' --noconfirm "${ALL_TARGETS[@]}" > "$TMP/closure.txt"; then
  die "Dependency resolution failed against snapshot $ARCHISO_DATE."
fi
[[ -s "$TMP/closure.txt" ]] || die "Dependency resolution produced an empty closure."

# Resolve each role independently.  These files later select exact, already
# hash-covered archives; safe triage therefore never installs a GPU driver.
if ! pac -Sp --print-format '%r %n %v %l' --noconfirm "${SAFE_TARGETS[@]}" > "$TMP/safe-closure.txt"; then
  die "Safe-runtime dependency resolution failed against snapshot $ARCHISO_DATE."
fi
if ! pac -Sp --print-format '%r %n %v %l' --noconfirm "${DRIVER_TARGETS[@]}" > "$TMP/driver-closure.txt"; then
  die "Driver-bound-runtime dependency resolution failed against snapshot $ARCHISO_DATE."
fi
CLOSURE_COUNT="$(wc -l < "$TMP/closure.txt")"
log "Closure: $CLOSURE_COUNT packages."

# Download the full closure. Only a subset is shipped, but resolving and
# downloading the whole set keeps this identical to what the live system would
# need if the ISO ever provided less than its package list claims. The cache is
# persistent, so a rebuild for the same snapshot re-downloads nothing.
log "Downloading packages (cache: $DL_CACHE)..."
# No host package is touched to make this work, so a stale keyring surfaces here
# rather than being silently repaired behind the user's back.
pac -Sw --noconfirm "${ALL_TARGETS[@]}" || die "Package download failed.
If pacman rejected signatures, the build system's keyring is older than the
snapshot: run 'sudo pacman -Sy archlinux-keyring' and try again."

# --- kernel version -------------------------------------------------------
# Read the module directory name straight out of the kernel package. That string
# is exactly what uname -r reports in the live system, so no version-string
# heuristic is needed.
LINUX_VERSION="$(awk '$2 == "linux" { print $3 }' "$TMP/closure.txt")"
[[ -n "$LINUX_VERSION" ]] || die "The closure contains no 'linux' package."
LINUX_FILE="$(awk '$2 == "linux" { n = split($4, p, "/"); print p[n] }' "$TMP/closure.txt")"
[[ -f "$DL_CACHE/$LINUX_FILE" ]] || die "Kernel package not in cache: $LINUX_FILE"

# sort -u rather than head -n1: head would close the pipe early and leave bsdtar
# with SIGPIPE, and it would hide a kernel package carrying two module trees.
mapfile -t KERNEL_DIRS < <(bsdtar -tf "$DL_CACHE/$LINUX_FILE" \
  | sed -n 's|^usr/lib/modules/\([^/]*\)/.*|\1|p' | sort -u)
[[ ${#KERNEL_DIRS[@]} -eq 1 ]] \
  || die "Expected exactly one module directory in $LINUX_FILE, found ${#KERNEL_DIRS[@]}: ${KERNEL_DIRS[*]:-none}"
EXPECTED_KERNEL="${KERNEL_DIRS[0]}"

# --- ISO package list -----------------------------------------------------
declare -A ISO_PKGS=()
ISO_PKG_COUNT=0
if [[ $ISO_SUBTRACT -eq 1 ]]; then
  log "Reading the package list out of $ISO_NAME..."
  if ! bsdtar -xOf "$ISO_PATH" arch/pkglist.x86_64.txt > "$TMP/isopkgs.txt" 2>"$TMP/isoerr.txt"; then
    die "Could not read arch/pkglist.x86_64.txt from the ISO: $(tr -d '\n' < "$TMP/isoerr.txt")
Use --no-iso-subtract to build the full closure instead."
  fi
  [[ -s "$TMP/isopkgs.txt" ]] || die "arch/pkglist.x86_64.txt in the ISO is empty."
  bundle_load_iso_packages "$TMP/isopkgs.txt" ISO_PKGS
  ISO_PKG_COUNT="$BUNDLE_ISO_PKG_COUNT"
  log "The ISO ships $ISO_PKG_COUNT packages."

  # The kernel is the one package where a version difference is fatal rather
  # than merely wasteful: nvidia-open builds modules for one exact kernel.
  ISO_LINUX="${ISO_PKGS[linux]:-}"
  if [[ -z "$ISO_LINUX" ]]; then
    warn "The ISO package list contains no 'linux' entry; the kernel cross-check is skipped."
  elif [[ "$ISO_LINUX" != "$LINUX_VERSION" ]]; then
    die "Kernel mismatch between ISO and archive snapshot.
  ISO $ISO_NAME ships: linux $ISO_LINUX
  Snapshot $ARCHISO_DATE has: linux $LINUX_VERSION
The NVIDIA modules would be built for the wrong kernel. Use the ISO whose date
matches the snapshot, or build with --no-iso-subtract and accept the mismatch."
  fi
fi

# --- split the closure ----------------------------------------------------
bundle_split_closure "$TMP/closure.txt" ISO_PKGS "$ISO_SUBTRACT" "$TMP/keep.txt" "$EXCLUDED"
EXCLUDED_COUNT="$BUNDLE_EXCLUDED_COUNT"
KEEP_COUNT="$BUNDLE_KEEP_COUNT"
[[ $KEEP_COUNT -gt 0 ]] || die "Nothing left to ship after subtracting the ISO packages."

mkdir -p "$PROFILES_DIR"
bundle_split_closure "$TMP/safe-closure.txt" ISO_PKGS "$ISO_SUBTRACT" "$TMP/safe-keep.txt" "$TMP/safe-excluded.txt"
awk -F '\t' 'NF >= 4 { print "packages/" $4 }' "$TMP/safe-keep.txt" | sed 's/:/_/g' | sort -u > "$PROFILES_DIR/safe-runtime.files"
bundle_split_closure "$TMP/driver-closure.txt" ISO_PKGS "$ISO_SUBTRACT" "$TMP/driver-keep.txt" "$TMP/driver-excluded.txt"
awk -F '\t' 'NF >= 4 { print "packages/" $4 }' "$TMP/driver-keep.txt" | sed 's/:/_/g' | sort -u > "$PROFILES_DIR/driver-bound-runtime.files"

# --- assemble the bundle --------------------------------------------------
log "Building the version-bound C17 Vulkan helper and SPIR-V..."
make -C "$REPO_ROOT/vram-helper" clean all OUT_DIR="$HELPER_DIR"
"$HELPER_DIR/gpu-triage-vram-helper" --self-test > "$TMP/helper-self-test.jsonl"
grep -q '"type":"summary"' "$TMP/helper-self-test.jsonl" \
  || die "Native helper self-test did not emit a summary."

log "Copying $KEEP_COUNT package(s) to $PKG_DIR..."
declare -A SEEN_FILES=()
RENAMED=0
while IFS=$'\t' read -r _ name _ file; do
  [[ -f "$DL_CACHE/$file" ]] || die "Package missing from cache: $file ($name)"
  # A package with an epoch carries a colon in its filename, and exFAT and NTFS
  # reject that — the bundle has to survive being copied onto the Ventoy data
  # partition and unpacked by Expand-Archive. pacman reads name and version from
  # the package metadata rather than the filename, so renaming is safe as long
  # as the detached signature is renamed with it.
  target="$(bundle_windows_filename "$file")"
  if [[ "$target" != "$file" ]]; then
    RENAMED=$((RENAMED + 1))
  fi
  [[ -z "${SEEN_FILES[$target]:-}" ]] || die "Filename collision after sanitising: $target"
  SEEN_FILES["$target"]=1
  cp -f "$DL_CACHE/$file" "$PKG_DIR/$target"
  # Signatures are not covered by SHA256SUMS, but pacman -U reads them when they
  # sit next to the package, so they travel along.
  if [[ -f "$DL_CACHE/$file.sig" ]]; then
    cp -f "$DL_CACHE/$file.sig" "$PKG_DIR/$target.sig"
  fi
done < "$TMP/keep.txt"
if [[ $RENAMED -gt 0 ]]; then
  log "Renamed $RENAMED package file(s) containing an epoch colon for Windows filesystems."
fi

BUNDLE_CREATED="$(date --iso-8601=seconds)"
cat > "$MANIFEST" <<MANIFEST
# Generated by offline/build_bundle.sh
ARCHISO_DATE='$ARCHISO_DATE'
EXPECTED_KERNEL='$EXPECTED_KERNEL'
BUNDLE_CREATED='$BUNDLE_CREATED'
ISO_SUBTRACT='$ISO_SUBTRACT'
BUNDLE_PACKAGES='$KEEP_COUNT'
MANIFEST

(
  cd "$REPO_ROOT/offline"
  find packages -maxdepth 1 -type f -name '*.pkg.tar.zst' -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
  find helper -type f -print0 | sort -z | xargs -0 sha256sum > HELPER-SHA256SUMS
  sha256sum manifest.env profiles/safe-runtime.files profiles/driver-bound-runtime.files HELPER-SHA256SUMS > PROFILE-SHA256SUMS
)

BUNDLE_SIZE="$(du -sh "$PKG_DIR" | awk '{print $1}')"

if [[ $WRITE_DIST -eq 1 ]]; then
  mkdir -p "$DIST_DIR"
  ZIP_NAME="gpu-triage-bundle-$ARCHISO_DATE.zip"
  rm -f "$DIST_DIR/$ZIP_NAME" "$DIST_DIR/$ZIP_NAME.sha256"
  # ZIP rather than tar.zst on purpose: Expand-Archive exists on every Windows
  # box, a zstd-capable tar does not. The packages are already compressed, so
  # the archive only wraps them.
  (
    cd "$REPO_ROOT/offline"
    bsdtar --format zip --options zip:compression=store \
           -cf "$DIST_DIR/$ZIP_NAME" packages profiles helper manifest.env SHA256SUMS HELPER-SHA256SUMS PROFILE-SHA256SUMS excluded.txt
  )
  (
    cd "$DIST_DIR"
    sha256sum "$ZIP_NAME" > "$ZIP_NAME.sha256"
  )
fi

# Root built it, but the repository belongs to whoever invoked the script.
OWNER="${GPU_TRIAGE_OWNER:-}"
if [[ -z "$OWNER" && -n "${SUDO_UID:-}" ]]; then
  OWNER="$SUDO_UID:${SUDO_GID:-$SUDO_UID}"
fi
if [[ -n "$OWNER" ]]; then
  chown -R "$OWNER" "$PKG_DIR" "$DL_CACHE" "$MANIFEST" "$EXCLUDED" \
        "$REPO_ROOT/offline/SHA256SUMS" "$PROFILE_SUMS" "$PROFILES_DIR" 2>/dev/null || true
  chown -R "$OWNER" "$HELPER_DIR" "$HELPER_SUMS" 2>/dev/null || true
  [[ -d "$DIST_DIR" ]] && chown -R "$OWNER" "$DIST_DIR" 2>/dev/null || true
fi

log "Bundle complete."
log "Expected live kernel: $EXPECTED_KERNEL"
log "Closure: $CLOSURE_COUNT | shipped: $KEEP_COUNT | provided by the ISO: $EXCLUDED_COUNT"
log "Size: $BUNDLE_SIZE"
log "Manifest: $MANIFEST"
[[ $WRITE_DIST -eq 1 ]] && log "Artifact: $DIST_DIR/$ZIP_NAME"
