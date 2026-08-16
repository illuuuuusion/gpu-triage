#!/usr/bin/env bash
# Hardware-free integration tests for go.sh mount/remount routing.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# A private mount namespace makes the read-only bind mount recoverable and
# ensures this test can never alter host mounts.
if [[ "${GPU_TRIAGE_GO_TEST_NS:-0}" != 1 ]]; then
  export GPU_TRIAGE_GO_TEST_NS=1
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    exec unshare -m bash "$0" "$@"
  fi
  exec unshare -rm bash "$0" "$@"
fi

FAILED=0
pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; FAILED=1; }
contains() {
  if grep -Fq -- "$2" <<< "$1"; then pass "$3"; else
    printf 'FAIL %s\n       missing: %s\n       output:  %s\n' "$3" "$2" "$1"
    FAILED=1
  fi
}

TMP="$(mktemp -d)"
cleanup() {
  mountpoint -q "$TMP/direct" 2>/dev/null && umount "$TMP/direct"
  mountpoint -q "$TMP/readonly" 2>/dev/null && umount "$TMP/readonly"
  rm -rf "$TMP"
}
trap cleanup EXIT

make_repo() {
  local root="$1"
  mkdir -p "$root/app"
  cp "$REPO_ROOT/go.sh" "$root/go.sh"
  : > "$root/app/gpu_diag.py"
  cat > "$root/start.sh" <<'EOF'
#!/usr/bin/env bash
printf 'START_ARGS='
printf '<%s>' "$@"
printf '\n'
EOF
  chmod +x "$root/start.sh"
  # In a rootless user namespace the temporary directory can retain the outer
  # uid's ownership; explicit mode bits keep this case about go.sh, not uid maps.
  chmod -R a+rwX "$root"
}

# Direct launch: the wrapper must preserve every argument byte-for-byte. Use a
# dedicated rw bind because some containers report the writable /tmp overlay's
# superblock as ro even though its upper layer accepts writes.
make_repo "$TMP/direct-source"
mkdir -p "$TMP/direct"
mount --bind "$TMP/direct-source" "$TMP/direct"
mount -o remount,rw,bind "$TMP/direct"
OUTPUT="$(bash "$TMP/direct/go.sh" quick --gpu 0000:03:00.0 --no-vram 2>&1)"
STATUS=$?
[[ $STATUS -eq 0 ]] && pass 'writable repository launches successfully' || fail 'writable repository launches successfully'
contains "$OUTPUT" 'START_ARGS=<quick><--gpu><0000:03:00.0><--no-vram>' 'arguments are forwarded unchanged'
umount "$TMP/direct"

# Read-only bind mount: go.sh must remount the containing mount and then launch.
make_repo "$TMP/source"
mkdir -p "$TMP/readonly"
mount --bind "$TMP/source" "$TMP/readonly"
mount -o remount,ro,bind "$TMP/readonly"
OUTPUT="$(bash "$TMP/readonly/go.sh" list 2>&1)"
STATUS=$?
[[ $STATUS -eq 0 ]] && pass 'read-only bind mount is recovered' || fail 'read-only bind mount is recovered'
contains "$OUTPUT" 'Remounted ' 'successful remount is reported'
contains "$OUTPUT" 'START_ARGS=<list>' 'remounted repository launches start.sh'
findmnt -no OPTIONS --target "$TMP/readonly" | grep -q '^rw' \
  && pass 'bind mount is read-write after recovery' || fail 'bind mount is read-write after recovery'
umount "$TMP/readonly"

# Candidate selection without hardware: fake lsblk exposes one labelled raw
# device and fake mount records which path the real go.sh selected.
mkdir -p "$TMP/stubs" "$TMP/caller" "$TMP/candidate/gpu-triage/app"
cp "$REPO_ROOT/go.sh" "$TMP/caller/go.sh"
cp "$TMP/direct-source/start.sh" "$TMP/candidate/gpu-triage/start.sh"
: > "$TMP/candidate/gpu-triage/app/gpu_diag.py"
cat > "$TMP/stubs/findmnt" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
cat > "$TMP/stubs/lsblk" <<'EOF'
#!/usr/bin/env bash
printf 'sdz1 GPU_TRIAGE_TEST_LABEL\n'
EOF
cat > "$TMP/stubs/mountpoint" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
cat > "$TMP/stubs/mount" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> '$TMP/mount.log'
exit 0
EOF
chmod +x "$TMP/stubs/"*
OUTPUT="$(PATH="$TMP/stubs:$PATH" GPU_TRIAGE_LABEL=GPU_TRIAGE_TEST_LABEL \
  GPU_TRIAGE_MOUNT="$TMP/candidate" bash "$TMP/caller/go.sh" --help 2>&1)"
STATUS=$?
[[ $STATUS -eq 0 ]] && pass 'labelled candidate launches successfully' || fail 'labelled candidate launches successfully'
contains "$(cat "$TMP/mount.log")" '-o rw /dev/sdz1' 'raw device candidate is selected from lsblk label'
contains "$OUTPUT" 'START_ARGS=<--help>' 'candidate path forwards arguments'

# An empty candidate set must be actionable and must not attempt a mount.
cat > "$TMP/stubs/lsblk" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
: > "$TMP/mount.log"
OUTPUT="$(PATH="$TMP/stubs:$PATH" GPU_TRIAGE_LABEL=GPU_TRIAGE_MISSING_LABEL \
  GPU_TRIAGE_MOUNT="$TMP/missing" bash "$TMP/caller/go.sh" list 2>&1)"
STATUS=$?
[[ $STATUS -eq 2 ]] && pass 'missing Ventoy candidate exits 2' || fail 'missing Ventoy candidate exits 2'
contains "$OUTPUT" "No partition labelled 'GPU_TRIAGE_MISSING_LABEL' found" 'missing candidate names the cause'
[[ ! -s "$TMP/mount.log" ]] && pass 'missing candidate does not call mount' || fail 'missing candidate does not call mount'

if [[ "$FAILED" -eq 0 ]]; then
  echo 'go.sh tests: PASS'
else
  echo 'go.sh tests: FAIL'
fi
exit "$FAILED"
