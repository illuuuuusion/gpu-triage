#!/usr/bin/env bash
set -euo pipefail

# One command on the diagnostic PC.
#
#   bash /mnt/ventoy/gpu-triage/go.sh [start.sh arguments]
#
# go.sh is a wrapper in front of start.sh, not a second entry point. It removes
# the two things that made booting a three-line affair:
#
#   1. The Ventoy data partition has to be mounted read-write before start.sh
#      can write reports. Ventoy or the live system may have mounted it
#      read-only somewhere already; this script remounts it instead of failing
#      halfway through a diagnostic run.
#   2. The partition is not always reachable under the name one would expect.
#      Ventoy shadows the partition that holds the booted ISO, so from inside
#      the live system /dev/sdX1 can refuse to mount while the device mapper
#      node /dev/mapper/sdX1 that Ventoy provides works. Both are tried.
#
# The very first mount cannot be avoided while this file lives on the data
# partition — for that single case BOOT.txt on the stick root carries the
# one-liner that mounts and calls this script.

LABEL="${GPU_TRIAGE_LABEL:-Ventoy}"
MOUNTPOINT="${GPU_TRIAGE_MOUNT:-/mnt/ventoy}"

log() { printf '[go] %s\n' "$*"; }
warn() { printf '[go] WARNING: %s\n' "$*" >&2; }
die() { printf '[go] ERROR: %s\n' "$*" >&2; exit 2; }

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# awk 'NR==1' rather than 'head -n1': head closes the pipe early, which leaves
# the writer with SIGPIPE and fails the whole script under 'set -o pipefail'.
first_line() { awk 'NR==1'; }

mount_field() {
  # $1 = findmnt column, $2 = path
  command -v findmnt >/dev/null 2>&1 || return 1
  findmnt -no "$1" --target "$2" 2>/dev/null | first_line
}

is_writable() {
  local dir="$1" opts
  opts="$(mount_field OPTIONS "$dir" || true)"
  if [[ -n "$opts" ]]; then
    [[ "${opts%%,*}" == "rw" ]] || return 1
  fi
  # access(W_OK) reports EROFS for a read-only filesystem even for root, so
  # this also stands on its own when findmnt is unavailable.
  [[ -w "$dir" ]]
}

is_repo() { [[ -f "$1/start.sh" && -f "$1/app/gpu_diag.py" ]]; }

become_root() {
  [[ ${EUID:-$(id -u)} -eq 0 ]] && return 0
  command -v sudo >/dev/null 2>&1 \
    || die "Mounting requires root (the Arch live environment normally starts a root shell)."
  exec sudo -E "$0" "$@"
}

launch() {
  local root="$1"
  shift
  log "Starting $root/start.sh"
  exec bash "$root/start.sh" "$@"
}

# --- 1. started from a copy of the repository -----------------------------
# The common case: someone mounted the stick and called this file on it.
if is_repo "$SELF_DIR"; then
  if is_writable "$SELF_DIR"; then
    launch "$SELF_DIR" "$@"
  fi

  become_root "$@"
  MP="$(mount_field TARGET "$SELF_DIR" || true)"
  if [[ -n "$MP" ]]; then
    # A normal filesystem mount is turned writable by 'remount,rw'. A bind
    # mount keeps its read-only flag unless 'bind' is repeated in the same
    # call, so both spellings are tried before giving up on this path.
    for remount_opts in rw rw,bind; do
      mount -o "remount,$remount_opts" "$MP" 2>/dev/null || continue
      if is_writable "$SELF_DIR"; then
        log "Remounted $MP read-write; reports can be written to the stick."
        launch "$SELF_DIR" "$@"
      fi
    done
  fi
  warn "${MP:-The filesystem of $SELF_DIR} is read-only and could not be remounted."
  warn "Looking for another way to reach the '$LABEL' partition."
fi

# --- 2. the partition is already mounted somewhere ------------------------
if command -v findmnt >/dev/null 2>&1; then
  while read -r target; do
    [[ -n "$target" ]] || continue
    is_repo "$target/gpu-triage" || continue
    is_writable "$target" || continue
    launch "$target/gpu-triage" "$@"
  done < <(findmnt -rno TARGET --source "LABEL=$LABEL" 2>/dev/null || true)
fi

# --- 3. mount it ourselves ------------------------------------------------
become_root "$@"

candidate_devices() {
  local dev name
  # Ventoy's device mapper node comes first: when the ISO was booted from this
  # very partition, it is the node that actually reads back the data.
  while read -r name; do
    [[ -n "$name" ]] || continue
    dev="/dev/$name"
    [[ -b "/dev/mapper/$name" ]] && printf '%s\n' "/dev/mapper/$name"
    printf '%s\n' "$dev"
  done < <(lsblk -rno NAME,LABEL 2>/dev/null | awk -v l="$LABEL" '$2 == l { print $1 }')
  if [[ -e "/dev/disk/by-label/$LABEL" ]]; then
    printf '%s\n' "/dev/disk/by-label/$LABEL"
  fi
}

mapfile -t DEVICES < <(candidate_devices | awk '!seen[$0]++')
[[ ${#DEVICES[@]} -gt 0 ]] || die "No partition labelled '$LABEL' found.
Is the Ventoy stick plugged in? Prepare it on the Windows PC with
tools\\prepare-usb.ps1 and check it with 'prepare-usb.ps1 -Check'."

mkdir -p "$MOUNTPOINT"
if mountpoint -q "$MOUNTPOINT" 2>/dev/null; then
  if is_repo "$MOUNTPOINT/gpu-triage"; then
    if is_writable "$MOUNTPOINT" || mount -o remount,rw "$MOUNTPOINT" 2>/dev/null; then
      launch "$MOUNTPOINT/gpu-triage" "$@"
    fi
    die "$MOUNTPOINT carries the repository but is read-only and cannot be remounted."
  fi
  die "$MOUNTPOINT is already in use by another filesystem. Unmount it or set GPU_TRIAGE_MOUNT."
fi

for dev in "${DEVICES[@]}"; do
  mount -o rw "$dev" "$MOUNTPOINT" 2>/dev/null || continue
  if is_repo "$MOUNTPOINT/gpu-triage" && is_writable "$MOUNTPOINT"; then
    log "Mounted $dev at $MOUNTPOINT."
    launch "$MOUNTPOINT/gpu-triage" "$@"
  fi
  umount "$MOUNTPOINT" 2>/dev/null || true
done

die "Found ${#DEVICES[@]} candidate device(s) for label '$LABEL' (${DEVICES[*]}),
but none of them could be mounted read-write with gpu-triage/start.sh on it.
Mount the partition by hand and call start.sh directly:
  mkdir -p $MOUNTPOINT && mount /dev/disk/by-label/$LABEL $MOUNTPOINT
  bash $MOUNTPOINT/gpu-triage/start.sh"
