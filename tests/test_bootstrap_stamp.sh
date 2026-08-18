#!/usr/bin/env bash
# Regression tests for the bootstrap session stamp keying (scripts/bootstrap.sh).
#
# The bundle_id() function is extracted from the real script and evaluated here,
# so these assertions track the shipped implementation. Running the full
# bootstrap needs root plus the Arch live environment and is out of scope.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOOTSTRAP="$REPO_ROOT/scripts/bootstrap.sh"
SAFE_PACKAGES="$REPO_ROOT/offline/package-list-safe.txt"

FAILED=0
check() {
  if [[ "$2" == "$3" ]]; then
    printf 'ok   %s\n' "$1"
  else
    printf 'FAIL %s\n       expected: %s\n       actual:   %s\n' "$1" "$3" "$2"
    FAILED=1
  fi
}

# Pull bundle_id() out of the real script.
FUNC="$(sed -n '/^bundle_id() {/,/^}/p' "$BOOTSTRAP")"
[[ -n "$FUNC" ]] || { echo "FAIL could not extract bundle_id() from $BOOTSTRAP"; exit 1; }
eval "$FUNC"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/a/offline" "$TMP/b/offline"
printf "EXPECTED_KERNEL='6.16.1-arch1-1'\n" > "$TMP/a/offline/manifest.env"
printf "abc123  packages/python.pkg.tar.zst\n" > "$TMP/a/offline/SHA256SUMS"
cp "$TMP/a/offline/manifest.env" "$TMP/b/offline/manifest.env"
cp "$TMP/a/offline/SHA256SUMS" "$TMP/b/offline/SHA256SUMS"

id_for() {
  REPO_ROOT="$1"
  MANIFEST="$1/offline/manifest.env"
  SUMS="$1/offline/SHA256SUMS"
  bundle_id
}

BASE="$(id_for "$TMP/a")"
check "id is a sha256 hex digest" "${#BASE}" "64"

check "identical bundle yields identical id" "$(id_for "$TMP/a")" "$BASE"

# Same content at a different mount path must invalidate the stamp.
OTHER_ROOT="$(id_for "$TMP/b")"
[[ "$OTHER_ROOT" != "$BASE" ]] \
  && echo "ok   different mount path changes id" \
  || { echo "FAIL different mount path changes id"; FAILED=1; }

# A rebuilt bundle changes manifest.env and/or SHA256SUMS.
printf "EXPECTED_KERNEL='6.17.0-arch1-1'\n" > "$TMP/a/offline/manifest.env"
CHANGED_MANIFEST="$(id_for "$TMP/a")"
[[ "$CHANGED_MANIFEST" != "$BASE" ]] \
  && echo "ok   changed manifest.env changes id" \
  || { echo "FAIL changed manifest.env changes id"; FAILED=1; }

printf "EXPECTED_KERNEL='6.16.1-arch1-1'\n" > "$TMP/a/offline/manifest.env"
printf "def456  packages/python.pkg.tar.zst\n" > "$TMP/a/offline/SHA256SUMS"
CHANGED_SUMS="$(id_for "$TMP/a")"
[[ "$CHANGED_SUMS" != "$BASE" ]] \
  && echo "ok   changed SHA256SUMS changes id" \
  || { echo "FAIL changed SHA256SUMS changes id"; FAILED=1; }

# Missing files must not crash the helper.
rm -f "$TMP/a/offline/manifest.env" "$TMP/a/offline/SHA256SUMS"
MISSING="$(id_for "$TMP/a")"
check "missing manifest/sums still produce a digest" "${#MISSING}" "64"

# The stamp must live on tmpfs, never on the USB stick.
grep -q '^STAMP_DIR="/run/gpu-triage"' "$BOOTSTRAP" \
  && echo "ok   stamp directory is under /run" \
  || { echo "FAIL stamp directory is under /run"; FAILED=1; }

grep -q 'GPU_TRIAGE_FORCE_BOOTSTRAP' "$BOOTSTRAP" \
  && echo "ok   force-bootstrap escape hatch present" \
  || { echo "FAIL force-bootstrap escape hatch present"; FAILED=1; }

if grep -Eq '^[[:space:]]*(modprobe|rmmod)([[:space:]]|$)|/sys/bus/pci/(drivers/.*/(bind|unbind)|rescan)|/reset([[:space:]]|$)' "$BOOTSTRAP"; then
  echo "FAIL bootstrap contains a reachable GPU driver/device action"
  FAILED=1
else
  echo "ok   bootstrap contains no module/bind/reset action"
fi

grep -q 'refusing an unverified offline installation' "$BOOTSTRAP" \
  && echo "ok   missing SHA256SUMS fails closed" \
  || { echo "FAIL missing SHA256SUMS does not fail closed"; FAILED=1; }

if grep -Eq '^[[:space:]]*(source|\.)[[:space:]]+.*MANIFEST' "$BOOTSTRAP"; then
  echo "FAIL bootstrap executes manifest.env as shell code"
  FAILED=1
else
  echo "ok   manifest.env is parsed as data, not sourced"
fi

if grep -Evi '^\s*(#|$|python$|pciutils$)' "$SAFE_PACKAGES" | grep -q .; then
  echo "FAIL safe-runtime direct package role contains unexpected packages"
  FAILED=1
else
  echo "ok   safe-runtime direct role is limited to python and pciutils"
fi

grep -q 'bootstrap.sh" --profile safe-runtime' "$REPO_ROOT/start.sh" \
  && echo "ok   start.sh requests only safe-runtime for Stage 0/1" \
  || { echo "FAIL start.sh does not select safe-runtime"; FAILED=1; }

if [[ $FAILED -eq 0 ]]; then
  echo "bootstrap stamp tests: PASS"
else
  echo "bootstrap stamp tests: FAIL"
fi
exit $FAILED
